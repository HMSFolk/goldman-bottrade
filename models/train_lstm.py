# models/train_lstm.py
"""
LSTM + Multi-Head Attention Training Pipeline
- รับ sequence ของ bars แทน single row
- Attention บอกว่า bar ไหนสำคัญที่สุด
- Walk-Forward Validation แบบ time-series
- บันทึก scaler พร้อม model เสมอ
"""

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ✅ FIX CRITICAL-1: setup_logging ก่อน import อื่น
from bot.setup_logging import setup_logging
setup_logging()

import logging
import json
import time
import joblib
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import (
    classification_report,
    f1_score,
    accuracy_score,
)

# ✅ FIX CRITICAL-2: get_config() แทน yaml.safe_load
from config import get_config
CFG = get_config()

log = logging.getLogger("models")

# ✅ FIX CRITICAL-3: absolute paths จาก project root
PROCESSED_DIR = _ROOT / CFG['paths']['data_processed']
MODELS_DIR    = _ROOT / CFG['paths']['models_saved']    # ✅ FIX CRITICAL-3
REPORTS_DIR   = _ROOT / CFG['paths']['reports']         # ✅ FIX CRITICAL-3

NON_FEATURE_COLS = {
    'open','high','low','close',
    'tick_volume','real_volume','spread',
    'label','future_return','risk_adj_return',
}

# ── Device ────────────────────────────────────────────────────
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
log.info(f"PyTorch device: {DEVICE}")


# ══════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════
@dataclass
class LSTMFoldResult:
    fold:          int
    train_start:   str
    train_end:     str
    test_start:    str
    test_end:      str
    train_size:    int
    test_size:     int
    accuracy:      float
    f1_macro:      float
    buy_precision: float
    buy_recall:    float
    best_epoch:    int
    train_loss:    float
    val_loss:      float


@dataclass
class LSTMTrainResult:
    symbol:      str
    timeframe:   str
    seq_len:     int
    n_features:  int
    n_samples:   int
    folds:       list = field(default_factory=list)
    model_path:  str  = ""
    trained_at:  str  = ""

    @property
    def mean_accuracy(self) -> float:
        return np.mean([f.accuracy for f in self.folds])

    @property
    def mean_f1(self) -> float:
        return np.mean([f.f1_macro for f in self.folds])

    @property
    def std_accuracy(self) -> float:
        return np.std([f.accuracy for f in self.folds])

    def is_acceptable(self) -> bool:
        return (
            self.mean_accuracy >= 0.50 and
            self.mean_f1       >= 0.40 and
            self.std_accuracy  <= 0.12
        )

    def summary(self) -> str:
        return (
            f"{self.symbol} | "
            f"acc={self.mean_accuracy:.3f}±{self.std_accuracy:.3f} | "
            f"f1={self.mean_f1:.3f} | "
            f"seq={self.seq_len} | features={self.n_features}"
        )


# ══════════════════════════════════════════════════════════════
# Neural Network Architecture
# ══════════════════════════════════════════════════════════════
class LSTMAttentionTrader(nn.Module):
    """
    LSTM + Multi-Head Attention สำหรับการเทรด

    Architecture:
    Input (seq_len, features)
        ↓
    Input Projection (features → hidden_size)
        ↓
    LSTM (2 layers, bidirectional optional)
        ↓
    Multi-Head Attention (โฟกัส bars สำคัญ)
        ↓
    Layer Norm + Dropout
        ↓
    Fully Connected (hidden → 64 → 3)
        ↓
    Output (3 classes: SELL/HOLD/BUY)
    """

    def __init__(
        self,
        input_size:   int,
        hidden_size:  int   = 128,
        num_layers:   int   = 2,
        num_heads:    int   = 4,
        dropout:      float = 0.3,
        num_classes:  int   = 3,
        bidirectional:bool  = False,
    ):
        super().__init__()

        self.hidden_size   = hidden_size
        self.num_layers    = num_layers
        self.bidirectional = bidirectional
        self.directions    = 2 if bidirectional else 1

        # ── Input Projection ──────────────────────────────────
        # แปลง input features เป็น hidden_size ก่อน
        # ทำให้ LSTM ทำงานใน latent space แทน raw feature space
        self.input_proj = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(),
            nn.Dropout(dropout * 0.5),
        )

        # ── LSTM ──────────────────────────────────────────────
        self.lstm = nn.LSTM(
            input_size   = hidden_size,
            hidden_size  = hidden_size,
            num_layers   = num_layers,
            batch_first  = True,       # (batch, seq, feature)
            dropout      = dropout if num_layers > 1 else 0,
            bidirectional= bidirectional,
        )

        lstm_output_size = hidden_size * self.directions

        # ── Multi-Head Attention ───────────────────────────────
        # num_heads ต้องหาร lstm_output_size ลงตัว
        actual_heads     = self._find_valid_heads(
            lstm_output_size, num_heads
        )
        self.attention   = nn.MultiheadAttention(
            embed_dim    = lstm_output_size,
            num_heads    = actual_heads,
            dropout      = dropout * 0.5,
            batch_first  = True,
        )
        self.attn_norm   = nn.LayerNorm(lstm_output_size)

        # ── Classifier Head ────────────────────────────────────
        self.classifier  = nn.Sequential(
            nn.LayerNorm(lstm_output_size),
            nn.Linear(lstm_output_size, 64),
            nn.GELU(),                 # GELU ดีกว่า ReLU สำหรับ attention
            nn.Dropout(dropout),
            nn.Linear(64, 32),
            nn.GELU(),
            nn.Dropout(dropout * 0.5),
            nn.Linear(32, num_classes),
        )

        # ── Weight Initialization ─────────────────────────────
        self._init_weights()

    @staticmethod
    def _find_valid_heads(embed_dim: int, requested: int) -> int:
        """หา num_heads ที่หาร embed_dim ลงตัว"""
        for h in [requested, requested-1, requested+1, 2, 1]:
            if h > 0 and embed_dim % h == 0:
                return h
        return 1

    def _init_weights(self):
        """Xavier initialization สำหรับ linear layers"""
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
            elif isinstance(module, nn.LSTM):
                for name, param in module.named_parameters():
                    if 'weight' in name:
                        nn.init.orthogonal_(param)
                    elif 'bias' in name:
                        nn.init.zeros_(param)

    def forward(
        self,
        x:             torch.Tensor,      # (batch, seq_len, features)
        return_attention: bool = False,
    ) -> torch.Tensor:

        batch_size = x.shape[0]

        # ── 1. Input Projection ───────────────────────────────
        x = self.input_proj(x)             # (batch, seq, hidden)

        # ── 2. LSTM ───────────────────────────────────────────
        lstm_out, (h_n, _) = self.lstm(x)
        # lstm_out: (batch, seq, hidden * directions)

        # ── 3. Multi-Head Attention ────────────────────────────
        # Self-attention: query = key = value = lstm_out
        attn_out, attn_weights = self.attention(
            lstm_out, lstm_out, lstm_out
        )
        # Residual connection + Layer Norm
        attn_out = self.attn_norm(attn_out + lstm_out)

        # ── 4. Pooling — เอาแค่ timestep สุดท้าย ─────────────
        # timestep สุดท้ายมีข้อมูลครบที่สุด (รู้ทั้ง sequence)
        last_step = attn_out[:, -1, :]    # (batch, hidden)

        # ── 5. Classify ────────────────────────────────────────
        logits    = self.classifier(last_step)   # (batch, 3)

        if return_attention:
            return logits, attn_weights

        return logits

    def get_attention_weights(
        self, x: torch.Tensor) -> np.ndarray:
        """ดู attention weights — bar ไหนสำคัญที่สุด"""
        self.eval()
        with torch.no_grad():
            _, weights = self.forward(
                x.unsqueeze(0).to(DEVICE),
                return_attention=True,
            )
        return weights.squeeze(0).cpu().numpy()


# ══════════════════════════════════════════════════════════════
# Dataset
# ══════════════════════════════════════════════════════════════
class TradingSequenceDataset(Dataset):
    """
    แปลง DataFrame เป็น sequences สำหรับ LSTM

    แต่ละ sample = (sequence ของ seq_len bars, label)
    ต้องเรียงตามเวลา — ห้าม shuffle ระหว่าง train/test split
    """

    def __init__(
        self,
        X:       np.ndarray,    # (n_samples, n_features) — scaled แล้ว
        y:       np.ndarray,    # (n_samples,) labels: 0, 1, 2
        seq_len: int = 120, ):
        self.seq_len = seq_len
        self.X       = torch.FloatTensor(X)
        self.y       = torch.LongTensor(y)

        # จำนวน valid samples = n_samples - seq_len
        self.n       = len(X) - seq_len

        if self.n <= 0:
            raise ValueError(
                f"ข้อมูลน้อยเกินไป ({len(X)} rows) "
                f"สำหรับ seq_len={seq_len}"
            )

    def __len__(self) -> int:
        min_len = min(len(self.X), len(self.y))
        self.X = self.X[:min_len]
        self.y = self.y[:min_len]
        self.n = min_len
        return len(self.y) - self.seq_len

    def __getitem__(self, idx: int):
        # --- ระบบเบรกฉุกเฉิน ป้องกันการตกขอบทุกกรณี ---
        max_idx = min(len(self.X), len(self.y)) - self.seq_len - 1
        if idx > max_idx:
            idx = max(0, max_idx)
        x_seq = self.X[idx : idx + self.seq_len]   # (seq_len, features)
        label = self.y[idx + self.seq_len]          # scalar
        return x_seq, label

def make_weighted_sampler(y: np.ndarray) -> WeightedRandomSampler:
    """
    สร้าง sampler ที่ balance class อัตโนมัติ
    ป้องกัน model predict HOLD ตลอดเวลา

    ข้อสำคัญ: ใช้ใน DataLoader ของ train เท่านั้น
    ห้ามใช้กับ validation/test
    """
    class_counts = np.bincount(y)
    class_weights= 1.0 / (class_counts + 1e-9)
    sample_weights = class_weights[y]

    return WeightedRandomSampler(
        weights     = torch.DoubleTensor(sample_weights),
        num_samples = len(y),
        replacement = True,   # sample ซ้ำได้
    )

# ══════════════════════════════════════════════════════════════
# Data Preparation
# ══════════════════════════════════════════════════════════════
def load_and_prepare(
    symbol:    str,
    timeframe: str = "M15",
) -> tuple[pd.DataFrame, pd.Series, list]:
    """โหลดและเลือก features สำหรับ LSTM"""
    path = PROCESSED_DIR / f"{symbol}_{timeframe}_features.parquet"
    if not path.exists():
        raise FileNotFoundError(f"ไม่พบ {path}")

    df = pd.read_parquet(path)
    df = df.dropna(subset=['label'])

    # LSTM ใช้แค่ numeric features
    feature_cols = [
        c for c in df.columns
        if c not in NON_FEATURE_COLS
        and not c.startswith('future_')
        and df[c].dtype in ['float64','float32','int64','int32']
    ]

    # ลบ NaN และ constant
    nan_pct      = df[feature_cols].isna().mean()
    feature_cols = [c for c in feature_cols if nan_pct[c] <= 0.20]

    variance     = df[feature_cols].var()
    feature_cols = [c for c in feature_cols if variance[c] > 0]

    X = df[feature_cols]
    y = df['label'].astype(int)

    # map label: -1,0,1 → 0,1,2
    y = y.map({-1: 0, 0: 1, 1: 2})

    log.info(
        f"LSTM data: {len(feature_cols)} features | "
        f"{len(X):,} samples | "
        f"SELL={( y==0).mean():.1%} "
        f"HOLD={(y==1).mean():.1%} "
        f"BUY={(y==2).mean():.1%}"
    )

    return X, y, feature_cols


# ══════════════════════════════════════════════════════════════
# Walk-Forward Training
# ══════════════════════════════════════════════════════════════
def walk_forward_train(
    X:          pd.DataFrame,
    y:          pd.Series,
    feature_cols: list,
    seq_len:    int   = 60,
    n_splits:   int   = 3,
    gap_bars:   int   = 96,
    epochs:     int   = 50,
    batch_size: int   = 128,
    hidden_size:int   = 128,
    num_layers: int   = 2,
    num_heads:  int   = 4,
    dropout:    float = 0.3,
    lr:         float = 1e-3,
) -> tuple:
    """
    Walk-Forward Training สำหรับ LSTM

    ใช้ n_splits=3 แทน 5 เพราะ LSTM train ช้ากว่า
    """
    tscv     = TimeSeriesSplit(n_splits=n_splits, gap=gap_bars)
    results  = []
    models   = []
    f1_scores= []

    log.info(
        f"Walk-Forward LSTM: {n_splits} folds | "
        f"seq={seq_len} | hidden={hidden_size} | "
        f"layers={num_layers} | heads={num_heads}"
    )
    log.info("=" * 60)

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        t0 = time.time()

        X_tr_raw = X.iloc[train_idx].values
        X_te_raw = X.iloc[test_idx].values
        y_tr     = y.iloc[train_idx].values
        y_te     = y.iloc[test_idx].values

        # !! สำคัญมาก: fit scaler บน train เท่านั้น !!
        # ห้าม fit บน test — information leak
        scaler = StandardScaler()
        X_tr   = scaler.fit_transform(X_tr_raw)
        X_te   = scaler.transform(X_te_raw)

        # Replace NaN ด้วย 0 หลัง scale
        X_tr   = np.nan_to_num(X_tr, nan=0.0)
        X_te   = np.nan_to_num(X_te, nan=0.0)

        # ── Dataset & DataLoader ───────────────────────────────
        train_ds = TradingSequenceDataset(X_tr, y_tr, seq_len)
        test_ds  = TradingSequenceDataset(X_te, y_te, seq_len)

        # Weighted sampler สำหรับ balance class
        sampler  = make_weighted_sampler(y_tr)

        train_dl = DataLoader(
            train_ds,
            batch_size = batch_size,
            sampler    = sampler,       # ใช้ sampler แทน shuffle
            num_workers= 0,
            pin_memory = DEVICE.type == 'cuda',
        )
        test_dl  = DataLoader(
            test_ds,
            batch_size = batch_size * 2,
            shuffle    = False,         # !! ห้าม shuffle test !!
            num_workers= 0,
        )

        # ── Model ─────────────────────────────────────────────
        model = LSTMAttentionTrader(
            input_size   = len(feature_cols),
            hidden_size  = hidden_size,
            num_layers   = num_layers,
            num_heads    = num_heads,
            dropout      = dropout,
            num_classes  = 3,
        ).to(DEVICE)

        # ── Optimizer & Scheduler ──────────────────────────────
        optimizer = optim.AdamW(
            model.parameters(),
            lr           = lr,
            weight_decay = 1e-4,
        )
        scheduler = optim.lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max = epochs,
            eta_min = lr * 0.01,
        )
        criterion = nn.CrossEntropyLoss(
            label_smoothing = 0.1,   # ลด overconfidence
            # ✅ FIX: เพิ่ม class weight ให้ loss function
            # เดิมมีแค่ WeightedSampler (balance sampling) แต่ loss ไม่ weighted
            # → LSTM ยังคิดว่า predict HOLD ถูกบ่อยกว่า SELL/BUY
            # ตอนนี้ loss ถ่วงน้ำหนัก SELL/BUY ให้หนักขึ้นด้วย
            weight = torch.tensor(
                [1.0 / (np.sum(y_tr == c) + 1e-9) for c in range(3)],
                dtype=torch.float32,
            ).to(DEVICE),
        )

        # ── Training Loop ──────────────────────────────────────
        best_f1        = 0.0
        best_state     = None
        best_epoch     = 0
        patience       = 10
        patience_count = 0
        train_losses   = []
        val_f1s        = []

        for epoch in range(epochs):
            # Train
            model.train()
            epoch_loss = 0.0
            n_batches  = 0

            for X_batch, y_batch in train_dl:
                X_batch = X_batch.to(DEVICE)
                y_batch = y_batch.to(DEVICE)

                optimizer.zero_grad()
                logits = model(X_batch)
                loss   = criterion(logits, y_batch)

                loss.backward()
                # Gradient clipping — ป้องกัน exploding gradient
                nn.utils.clip_grad_norm_(
                    model.parameters(), max_norm=1.0
                )
                optimizer.step()

                epoch_loss += loss.item()
                n_batches  += 1

            scheduler.step()
            avg_loss = epoch_loss / max(n_batches, 1)
            train_losses.append(avg_loss)

            # Validate
            val_f1 = _evaluate_model(model, test_dl)
            val_f1s.append(val_f1)

            # Early stopping
            if val_f1 > best_f1:
                best_f1    = val_f1
                best_state = {
                    k: v.clone()
                    for k, v in model.state_dict().items()
                }
                best_epoch     = epoch + 1
                patience_count = 0
            else:
                patience_count += 1

            if (epoch + 1) % 10 == 0:
                log.debug(
                    f"  Fold {fold+1} Epoch {epoch+1}/{epochs}: "
                    f"loss={avg_loss:.4f} f1={val_f1:.4f} "
                    f"best={best_f1:.4f}"
                )

            if patience_count >= patience:
                log.debug(
                    f"  Early stop at epoch {epoch+1} "
                    f"(patience={patience})"
                )
                break

        # โหลด best weights
        if best_state:
            model.load_state_dict(best_state)

        # ── Final Evaluation ──────────────────────────────────
        model.eval()
        y_pred_list = []
        y_true_list = []

        with torch.no_grad():
            for X_batch, y_batch in test_dl:
                logits    = model(X_batch.to(DEVICE))
                preds     = logits.argmax(dim=1).cpu().numpy()
                y_pred_list.extend(preds)
                y_true_list.extend(y_batch.numpy())

        y_pred = np.array(y_pred_list)
        y_true = np.array(y_true_list)

        # map กลับ: 0,1,2 → -1,0,1 สำหรับ report
        label_map  = {0: -1, 1: 0, 2: 1}
        y_pred_orig= np.vectorize(label_map.get)(y_pred)
        y_true_orig= np.vectorize(label_map.get)(y_true)

        acc = accuracy_score(y_true_orig, y_pred_orig)
        f1  = f1_score(y_true_orig, y_pred_orig,
                       average='macro', zero_division=0)

        report  = classification_report(
            y_true_orig, y_pred_orig,
            target_names  = ['SELL','HOLD','BUY'],
            output_dict   = True,
            zero_division = 0,
        )
        buy_p  = report.get('BUY',{}).get('precision', 0)
        buy_r  = report.get('BUY',{}).get('recall',    0)
        elapsed= time.time() - t0

        fold_result = LSTMFoldResult(
            fold          = fold + 1,
            train_start   = str(X.index[train_idx[0]])[:10],
            train_end     = str(X.index[train_idx[-1]])[:10],
            test_start    = str(X.index[test_idx[0]])[:10],
            test_end      = str(X.index[test_idx[-1]])[:10],
            train_size    = len(X_tr),
            test_size     = len(X_te),
            accuracy      = round(acc, 4),
            f1_macro      = round(f1,  4),
            buy_precision = round(buy_p, 4),
            buy_recall    = round(buy_r, 4),
            best_epoch    = best_epoch,
            train_loss    = round(train_losses[-1], 4),
            val_loss      = round(1 - best_f1,      4),
        )
        results.append(fold_result)
        f1_scores.append(f1)
        models.append((model, scaler))

        log.info(
            f"Fold {fold+1}/{n_splits}: "
            f"acc={acc:.3f} f1={f1:.3f} "
            f"BUY_p={buy_p:.3f} BUY_r={buy_r:.3f} "
            f"epoch={best_epoch} ({elapsed:.0f}s)"
        )

    # สรุป
    accs = [r.accuracy for r in results]
    f1s  = [r.f1_macro for r in results]
    log.info("=" * 60)
    log.info(
        f"LSTM Summary: "
        f"acc={np.mean(accs):.3f}±{np.std(accs):.3f} | "
        f"f1={np.mean(f1s):.3f}±{np.std(f1s):.3f}"
    )

    best_idx           = int(np.argmax(f1_scores))
    best_model, best_scaler = models[best_idx]
    log.info(f"Best: fold {best_idx+1} (f1={f1_scores[best_idx]:.4f})")

    return best_model, best_scaler, results


def _evaluate_model(
    model:    nn.Module,
    loader:   DataLoader,
) -> float:
    """Evaluate F1 macro บน DataLoader"""
    model.eval()
    preds_all = []
    labels_all= []

    with torch.no_grad():
        for X_b, y_b in loader:
            logits = model(X_b.to(DEVICE))
            preds  = logits.argmax(dim=1).cpu().numpy()
            preds_all.extend(preds)
            labels_all.extend(y_b.numpy())

    return f1_score(
        labels_all, preds_all,
        average      = 'macro',
        zero_division = 0,
    )


# ══════════════════════════════════════════════════════════════
# Main Training Function
# ══════════════════════════════════════════════════════════════
def train_lstm(
    symbol:      str,
    timeframe:   str   = "M15",
    seq_len:     int   = 60,
    n_splits:    int   = 3,
    epochs:      int   = 50,
    batch_size:  int   = 128,
    hidden_size: int   = 128,
    num_layers:  int   = 2,
    num_heads:   int   = 4,
    dropout:     float = 0.3,
    lr:          float = 1e-3,
) -> LSTMTrainResult:
    """Train LSTM สำหรับ 1 symbol"""

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)

    log.info(f"\n{'='*60}")
    log.info(f"Training LSTM+Attention: {symbol}_{timeframe}")
    log.info(f"Device: {DEVICE}")
    log.info(f"{'='*60}")

    t_start = time.time()

    # ── Load ───────────────────────────────────────────────────
    X, y, feature_cols = load_and_prepare(symbol, timeframe)

    # ── Walk-Forward ───────────────────────────────────────────
    gap_bars   = CFG['training']['wf_gap_bars']
    
    min_len = min(len(X), len(y))
    X = X[:min_len]
    y = y[:min_len]

    model, scaler, fold_results = walk_forward_train(
        X, y, feature_cols,
        seq_len     = seq_len,
        n_splits    = n_splits,
        gap_bars    = gap_bars,
        epochs      = epochs,
        batch_size  = batch_size,
        hidden_size = hidden_size,
        num_layers  = num_layers,
        num_heads   = num_heads,
        dropout     = dropout,
        lr          = lr,
    )

    # ── Save ───────────────────────────────────────────────────
    model_path = _save_model(
        model, scaler, feature_cols,
        {
            'seq_len'    : seq_len,
            'hidden_size': hidden_size,
            'num_layers' : num_layers,
            'num_heads'  : num_heads,
            'dropout'    : dropout,
        },
        symbol, timeframe,
    )

    # ── Result ─────────────────────────────────────────────────
    result = LSTMTrainResult(
        symbol     = symbol,
        timeframe  = timeframe,
        seq_len    = seq_len,
        n_features = len(feature_cols),
        n_samples  = len(X),
        folds      = fold_results,
        model_path = str(model_path),
        trained_at = datetime.now(timezone.utc).isoformat(),
    )

    elapsed = time.time() - t_start
    log.info(f"\n✅ เสร็จ ({elapsed:.0f}s): {result.summary()}")
    if not result.is_acceptable():
        log.warning("⚠️ โมเดลยังไม่ผ่านเกณฑ์")

    _save_report(result, symbol, timeframe)
    return result


# ══════════════════════════════════════════════════════════════
# Save & Load
# ══════════════════════════════════════════════════════════════
def _save_model(
    model:        nn.Module,
    scaler:       StandardScaler,
    features:     list,
    arch_params:  dict,
    symbol:       str,
    timeframe:    str,
) -> Path:
    """บันทึก model weights + scaler + metadata"""
    payload = {
        'model_state' : model.state_dict(),
        'arch_params' : arch_params,
        'scaler'      : scaler,
        'features'    : features,
        'meta'        : {
            'symbol'    : symbol,
            'timeframe' : timeframe,
            'n_features': len(features),
            'trained_at': datetime.now(timezone.utc).isoformat(),
            'device'    : str(DEVICE),
            'torch_ver' : torch.__version__,
        },
    }

    path = MODELS_DIR / f"lstm_{symbol}.pth"
    torch.save(payload, path)
    log.info(f"💾 บันทึก → {path}")
    return path


def load_model(symbol: str) -> tuple:
    """โหลด LSTM model พร้อม scaler"""
    path = MODELS_DIR / f"lstm_{symbol}.pth"
    if not path.exists():
        raise FileNotFoundError(f"ไม่พบ {path}")

    # ✅ FIX MEDIUM: weights_only=False สำหรับ dict payload (PyTorch 2.4+ ต้องระบุ)
    payload    = torch.load(
        path,
        map_location = DEVICE,
        weights_only = False,
    )
    arch       = payload['arch_params']
    features   = payload['features']

    model = LSTMAttentionTrader(
        input_size  = len(features),
        hidden_size = arch['hidden_size'],
        num_layers  = arch['num_layers'],
        num_heads   = arch['num_heads'],
        dropout     = arch['dropout'],
    ).to(DEVICE)

    model.load_state_dict(payload['model_state'])
    model.eval()

    log.info(
        f"โหลด LSTM {symbol}: "
        f"{len(features)} features | "
        f"seq={arch['seq_len']} | "
        f"trained {payload['meta']['trained_at'][:10]}"
    )

    return model, payload['scaler'], payload['arch_params'], features


def predict(
    symbol:   str,
    df:       pd.DataFrame,
    min_conf: float = 0.60,
) -> dict:
    """Predict จาก sequence ล่าสุด"""
    model, scaler, arch, features = load_model(symbol)
    seq_len  = arch['seq_len']

    avail    = [f for f in features if f in df.columns]
    X_raw    = df[avail].tail(seq_len + 10).values
    X_scaled = scaler.transform(X_raw)
    X_scaled = np.nan_to_num(X_scaled, nan=0.0)

    # เอา seq_len bars สุดท้าย
    X_seq    = torch.FloatTensor(
        X_scaled[-seq_len:]
    ).unsqueeze(0).to(DEVICE)   # (1, seq_len, features)

    with torch.no_grad():
        logits = model(X_seq)
        proba  = torch.softmax(logits, dim=-1).cpu().numpy()[0]

    pred_idx      = int(proba.argmax())
    conf          = float(proba[pred_idx])
    direction_map = {0: -1, 1: 0, 2: 1}
    direction     = direction_map[pred_idx] if conf >= min_conf else 0

    return {
        'direction'  : direction,
        'confidence' : round(conf,         4),
        'proba_sell' : round(float(proba[0]), 4),
        'proba_hold' : round(float(proba[1]), 4),
        'proba_buy'  : round(float(proba[2]), 4),
    }


def _save_report(result: LSTMTrainResult, symbol: str, timeframe: str):
    report = {
        'symbol'      : result.symbol,
        'seq_len'     : result.seq_len,
        'n_features'  : result.n_features,
        'trained_at'  : result.trained_at,
        'is_acceptable': result.is_acceptable(),
        'mean_accuracy': round(result.mean_accuracy, 4),
        'mean_f1'     : round(result.mean_f1,        4),
        'std_accuracy': round(result.std_accuracy,   4),
        'folds'       : [
            {
                'fold'        : f.fold,
                'train_period': f"{f.train_start}→{f.train_end}",
                'test_period' : f"{f.test_start}→{f.test_end}",
                'accuracy'    : f.accuracy,
                'f1_macro'    : f.f1_macro,
                'best_epoch'  : f.best_epoch,
                'train_loss'  : f.train_loss,
            }
            for f in result.folds
        ],
    }
    out = REPORTS_DIR / f"train_report_lstm_{symbol}_{timeframe}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

# ══════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols",    nargs="+",
                        default=CFG['symbols']['active'])
    parser.add_argument("--timeframe",  default="M15")
    parser.add_argument("--seq_len",    type=int,   default=60)
    parser.add_argument("--epochs",     type=int,   default=50)
    parser.add_argument("--hidden",     type=int,   default=128)
    parser.add_argument("--layers",     type=int,   default=2)
    parser.add_argument("--heads",      type=int,   default=4)
    parser.add_argument("--batch",      type=int,   default=128)
    parser.add_argument("--lr",         type=float, default=1e-3)
    args = parser.parse_args()

    for sym in args.symbols:
        try:
            train_lstm(
                symbol      = sym,
                timeframe   = args.timeframe,
                seq_len     = args.seq_len,
                epochs      = args.epochs,
                hidden_size = args.hidden,
                num_layers  = args.layers,
                num_heads   = args.heads,
                batch_size  = args.batch,
                lr          = args.lr,
            )
        except Exception as e:
            log.error(f"❌ {sym}: {e}", exc_info=True)