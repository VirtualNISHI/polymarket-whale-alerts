# Polymarket Whale Alerts Bot

Polymarketの大口建玉・板異常を検知してDiscordに通知するボット。
仮想NISHI配信の **Phase 1**(最初に稼働させるシステム)。

データソースは **Polymarket 公開 API のみ**(Gamma / CLOB / Data)。認証不要・無料。

## このプロジェクトの目的

- 出来高急増マーケットを定期スクリーニング
- 大口取引($50k+)、板の偏りを検知
- 該当ウォレットの Polymarket 内 PnL を集計してタグ付け(Smart Trader / Whale / New)
- Discord Embedで速報配信
- 検知履歴をSQLiteに蓄積し、後日のヒット率検証に使う

## アーキテクチャ

```
[VPS cron (5–15分間隔)]
   ↓
[Gamma API: /events?order=volume_24hr]
   ↓
[出来高上位マーケットを抽出 (CLOB enabled のみ)]
   ↓
[CLOB /book で YES/NO の板を取得]
[Data API /trades で直近1時間の約定を取得]
   ↓
[閾値判定(大口取引・板偏り)]
   ↓
[該当ウォレットを Data API /positions で属性判定]
   ↓
[SQLite に通知済みフラグを記録(重複防止)]
   ↓
[Discord Webhook POST]
```

## ディレクトリ構成

```
polymarket-whale-alerts/
├── README.md             # このファイル
├── SPEC.md               # 詳細仕様
├── pyproject.toml
├── .env.example
├── config/
│   └── thresholds.yaml   # 検知閾値
├── src/
│   ├── __init__.py
│   ├── polymarket_client.py
│   ├── discord_client.py
│   ├── db.py
│   ├── detector.py       # 閾値判定ロジック
│   ├── formatter.py      # Discord Embed 整形
│   ├── config.py         # .env / yaml ローダ
│   └── job.py            # メインジョブ
├── scripts/
│   └── run.py            # cron から呼ぶエントリポイント
├── tests/
│   ├── test_db.py
│   ├── test_detector.py
│   ├── test_formatter.py
│   ├── test_job.py
│   └── test_polymarket_client.py
└── data/
    └── whale_alerts.db   # SQLite(.gitignore)
```

## セットアップ(VPS)

1. `git clone` してこのディレクトリへ
2. `python3.11 -m venv .venv && source .venv/bin/activate`
3. `pip install -e ".[dev]"`
4. `cp .env.example .env` して値を埋める
   - `DISCORD_WEBHOOK_URL`(個人検証サーバーの `#whale-alerts-poly` チャンネル)
   - Polymarket 側は認証不要なので追加キーは不要
5. 初回 DB 作成: `python -m src.db init`
6. 動作確認: `python scripts/run.py --dry-run`
7. cron 登録: `*/10 * * * * cd /path/to/polymarket-whale-alerts && .venv/bin/python scripts/run.py >> data/run.log 2>&1`

## 開発フェーズ

仕様変更があれば `SPEC.md` を更新してから着手する。実装順は概ね以下:

1. **DB スキーマ実装** (`src/db.py`)
2. **Polymarket クライアント実装** (`src/polymarket_client.py`)
3. **Discord クライアント実装** (`src/discord_client.py`)
4. **閾値判定ロジック** (`src/detector.py`)
5. **Embed 整形** (`src/formatter.py`)
6. **メインジョブ** (`src/job.py`)
7. **エントリポイント** (`scripts/run.py`)
8. **動作確認スクリプト** (dry-run モード)

## 運用ポリシー

- **2 週間**動かして閾値チューニング
- ヒット率(その後の確率変動方向の正答率)を別途集計
- 信頼できるシグナル種別だけ後段(Smart Money / Macro Divergence)と統合検討
- 配信先は **個人検証用 Discord サーバー限定**(外部公開しない)

## Polymarket API について

- 全エンドポイント無料・無認証
- レート制限はあるが 10 分 cron + 1 リクエスト 200ms 間隔で実用上問題なし
- Phase 1 ではウォレット属性判定が **Polymarket 内 PnL のみ** に限定される(Nansen 等のクロスプロトコル判定は Phase 2 以降で検討)
- 2026 年 5 月時点で確認した API パスを使用。Polymarket 側の仕様変更に追随する場合は `src/polymarket_client.py` を調整

## 関連プロジェクト(別スレッドで管理)

- `smart-money/` — Phase 2: 常勝ウォレット追跡
- `macro-divergence/` — Phase 3: 確率 vs 実勢乖離分析
