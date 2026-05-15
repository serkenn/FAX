# WEFAX Decoder

RTL-SDR を使って HF 気象 FAX（WEFAX）を受信・デコードする Python 製デスクトップアプリです。

従来の音声入力（サウンドカード）方式を廃し、RTL-SDR ドングルから直接 IQ サンプルを取得します。  
HF 帯受信には RTL-SDR の **Q ブランチ ダイレクトサンプリング** モードを使用します。

---

## スクリーンショット

受信中は行単位でリアルタイム描画され、受信完了後に PNG/JPEG で保存できます。

---

## 動作原理

```
RTL-SDR IQ @ 240 kHz
  → AM 包絡線検波
  → FIR 低域通過 + 10 倍デシメーション → 24 kHz 音声
  → バンドパスフィルタ (1400–2500 Hz)
  → 1900 Hz NCO 混合 → 複素ベースバンド
  → 位相微分 → 瞬時周波数
  → グレースケール変換 (1500 Hz = 黒 / 2300 Hz = 白)
  → ピクセル組立 → 画像表示
```

| パラメータ | 値 |
|---|---|
| サンプルレート (RTL-SDR) | 240,000 sps |
| 音声レート (デシメーション後) | 24,000 sps |
| FM サブキャリア | 1500–2300 Hz |
| 標準速度 | 120 LPM / 1809 pixels/line |
| スタートトーン | 300 Hz |
| ストップトーン | 450 Hz |

---

## 主な受信周波数

| 局名 | 周波数 |
|---|---|
| JMH 気象庁（東京） | 3622.5 / 7795 / 13988 kHz |
| NMF ボストン（米国） | 4610 / 8110 kHz |
| CFH ハリファックス（カナダ） | 4271 / 6496.4 kHz |
| DDH ハンブルク（ドイツ） | 3855 / 7880 kHz |

---

## 必要なもの

- Python 3.10 以上
- RTL-SDR ドングル（R820T / R820T2 推奨）
- **librtlsdr**（OS 別にインストール）

### librtlsdr のインストール

```bash
# macOS
brew install librtlsdr

# Ubuntu / Debian
sudo apt install librtlsdr-dev libusb-1.0-0-dev

# Arch Linux
sudo pacman -S rtl-sdr
```

### Python 依存パッケージ

```bash
pip install -r requirements.txt
```

---

## 使い方

### GUI 起動

```bash
python main.py
```

### CLI オプションで起動

```bash
# 受信周波数をあらかじめ指定
python main.py --freq 8140

# デバイス番号・ゲインを指定
python main.py --freq 7795 --device 0 --gain 30

# VHF/UHF（ダイレクトサンプリング不要）
python main.py --freq 137100 --no-direct-sampling
```

| オプション | 説明 | デフォルト |
|---|---|---|
| `--freq KHZ` | 受信周波数 (kHz) | GUI で設定 |
| `--device N` | RTL-SDR デバイス番号 | `0` |
| `--gain DB` | チューナーゲイン (dB) または `auto` | `auto` |
| `--no-direct-sampling` | ダイレクトサンプリングを無効化 | 無効 (HF 向け ON) |

### GUI の操作

1. **Direct sampling – Q branch** にチェック（HF 受信時、デフォルト ON）
2. プリセットまたは周波数欄に受信周波数を入力
3. **▶ Start** をクリック → 300 Hz スタートトーンを待機
4. 受信が始まると行単位でリアルタイム描画
5. 受信完了後 **Save…** で PNG / JPEG に保存

---

## バイナリリリース

タグ付き [Releases](../../releases) から各プラットフォームの実行ファイルをダウンロードできます。

| ファイル | プラットフォーム |
|---|---|
| `wefax-linux-x86_64-*.zip` | Linux x86_64 |
| `wefax-macos-arm64-*.zip` | macOS Apple Silicon |
| `wefax-macos-x86_64-*.zip` | macOS Intel |

> **注意:** バイナリを使う場合も `librtlsdr` のシステムインストールは必要です。

---

## 開発

```
.
├── main.py           # エントリポイント・CLI 引数
├── gui.py            # tkinter GUI
├── fax_decoder.py    # WEFAX FM 復調・ステートマシン
├── rtlsdr_source.py  # RTL-SDR IQ 取得・AM 検波・AGC
├── requirements.txt
└── .github/
    └── workflows/
        └── release.yml   # タグ push → 自動ビルド & リリース
```

### CI / リリース

`v` から始まるタグを push すると GitHub Actions が自動的に:

1. Linux / macOS (arm64・x86_64) で PyInstaller ビルド
2. GitHub Release を作成してバイナリを添付

```bash
git tag v1.0.0
git push origin v1.0.0
```

---

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照してください。
