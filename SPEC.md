# Whale Alerts — 詳細仕様

データソースは **Polymarket 公開 API**(Gamma / CLOB / Data)のみ。認証不要。

## 1. 検知ロジック

### 1.1 マーケットスクリーニング

Gamma API `GET https://gamma-api.polymarket.com/events` を以下のクエリで叩く:

```
?active=true
&closed=false
&archived=false
&order=volume_24hr
&ascending=false
&volume_min={min_volume_usd}
&limit={N*4}
```

返ってきた events をフラット化(各 event の `markets[]` を展開)し、以下を満たすマーケットだけ残す:

- `closed != true` かつ `archived != true` かつ `active != false`
- `enableOrderBook != false` (CLOB マーケットのみ。AMM-only は除外)
- `volume24hr >= min_volume_usd`
- `category` が `excluded_categories` に含まれない

各マーケットを `volume24hr` 降順でソートして上位 `top_n_markets` 件(初期値 50)を後段に渡す。

### 1.2 大口取引検知

Data API `GET https://data-api.polymarket.com/trades?market={conditionId}&takerOnly=false&limit=500` で直近の約定を取得。各約定の **USD 額** は `size * price`(`size` はシェア数、`price` は 0..1)で計算する。

前回ポーリング時刻以降の取引のうち、以下を「大口取引」として扱う:

- 単一取引の USD 額が `min_trade_usd` 以上(初期値 $50,000)
- または、同一ウォレット(`proxyWallet`)の直近 1 時間累計が `min_trade_usd_hourly` 以上(初期値 $100,000)

### 1.3 板の偏り検知

各マーケットの `clobTokenIds` から YES / NO のトークン ID を取り出し、それぞれ CLOB API `GET https://clob.polymarket.com/book?token_id={id}` で板を取得。

- YES 側流動性 = YES 板の bids について `Σ(price × size)`
- NO 側流動性 = NO 板の bids について `Σ(price × size)`
- 流動性比 = max / min

以下をすべて満たした場合に「板異常」アラート:

- 流動性比が `orderbook_skew_ratio` 倍以上(初期値 3 倍)
- 確率の 1 時間変動 `|oneHourPriceChange| >= prob_change_1h`(初期値 0.05 = 5%pt)
- 直近 1 時間の累計 USD 出来高が `min_volume_1h` 以上(初期値 $20,000)

`oneHourPriceChange` は Gamma API のマーケットフィールドにそのまま含まれているので、別途 OHLCV を取得する必要はない。

### 1.4 ウォレット属性判定

検知された大口取引のウォレット(`proxyWallet`)について、Data API `GET https://data-api.polymarket.com/positions?user={address}&sizeThreshold=0&limit=500` で **現在保有中のポジション** を取得し、以下を集計:

- `cumulative_pnl_usd` = `Σ cashPnl`
- `cumulative_volume_usd` = `Σ totalBought`(無ければ `initialValue` で代替)
- `trade_count` = ポジション件数
- `win_rate` = `count(cashPnl > 0) / 件数`

判定ルール(`config/thresholds.yaml`):

- 取引数(=ポジション数)< `new.max_trade_count` → **New**
- それ以外で `cum_pnl >= smart_trader.min_cumulative_pnl_usd` かつ `win_rate >= smart_trader.min_win_rate` → **Smart Trader**
- それ以外で `cum_volume >= whale.min_cumulative_volume_usd` → **Whale**
- それ以外 → **Regular**

タグごとに Embed の色を変える。

> **注意:** Polymarket 内のオープンポジションだけを集計するため、Nansen のクロスプロトコル判定よりシグナルが弱くなる。閾値は 2 週間運用してヒット率を見ながら調整する。クローズ済みポジションを含めたい場合は `/closed-positions` を別途叩く実装に拡張する。

## 2. SQLite スキーマ

```sql
-- 通知済みアラート(重複防止)
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,       -- 'large_trade' | 'orderbook_skew'
    market_id TEXT NOT NULL,        -- conditionId
    market_slug TEXT,
    wallet_address TEXT,            -- large_trade のみ
    trade_id TEXT,                  -- transactionHash(重複防止のキー)
    amount_usd REAL,
    side TEXT,                      -- 'BUY YES' / 'SELL NO' / 'YES'(skew)
    probability REAL,               -- 検知時点の確率
    wallet_tag TEXT,                -- 'Smart Trader' | 'Whale' | 'New' | 'Regular'
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_at TIMESTAMP,
    raw_payload TEXT                -- JSON 文字列(後検証用)
);

CREATE UNIQUE INDEX idx_alerts_trade ON alerts(trade_id) WHERE trade_id IS NOT NULL;
CREATE INDEX idx_alerts_market ON alerts(market_id);
CREATE INDEX idx_alerts_detected ON alerts(detected_at);

-- ポーリング状態
CREATE TABLE poll_state (
    job_name TEXT PRIMARY KEY,
    last_polled_at TIMESTAMP,
    last_seen_trade_id TEXT
);

-- ウォレット属性キャッシュ(API 呼び出し削減)
CREATE TABLE wallet_cache (
    wallet_address TEXT PRIMARY KEY,
    tag TEXT,
    cumulative_pnl_usd REAL,
    win_rate REAL,
    trade_count INTEGER,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 後検証用:検知後の確率推移
CREATE TABLE alert_outcomes (
    alert_id INTEGER PRIMARY KEY,
    prob_at_1h REAL,
    prob_at_6h REAL,
    prob_at_24h REAL,
    final_resolution TEXT,          -- 'YES' | 'NO' | NULL(未確定)
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);
```

ウォレットキャッシュの TTL は 24 時間。

## 3. Discord Embed フォーマット

### 3.1 大口取引アラート

```
🐋 Whale Trade | Polymarket
━━━━━━━━━━━━━━━━━━━━
Market: {market_question}
Current Probability: {current_prob}% ({delta_24h}%pt 24h)
Trade: {action} {outcome} ${amount_usd:,.0f} @ ${entry_price}   (例: BUY YES $75,000 @ $0.62)
Wallet: {wallet_short} ({wallet_tag})
  Cumulative PnL: ${cum_pnl:,.0f}
  Win Rate: {win_rate:.0%} over {trade_count} trades
[View Market]({market_url}) | [View Wallet]({nansen_url})
```

色:
- Smart Trader → `0x2ecc71`(緑)
- Whale → `0x3498db`(青)
- New → `0xf39c12`(オレンジ)
- Regular → `0x95a5a6`(グレー)

`market_url` は `https://polymarket.com/event/{slug}` 、wallet 表示用リンクは Nansen Profiler URL を流用(`https://app.nansen.ai/profiler/{address}` — 認証不要で誰でも閲覧可)。

### 3.2 板異常アラート

```
⚖️ Orderbook Skew | Polymarket
━━━━━━━━━━━━━━━━━━━━
Market: {market_question}
Probability: {current_prob}% ({prob_change_1h:+.1f}%pt 1h)
Liquidity Skew: {dominant_side} side {skew_ratio:.1f}x deeper
Recent Volume: ${volume_1h:,.0f} (1h)
[View Market]({market_url})
```

色: `0xe67e22`(板異常はオレンジ固定)

## 4. 設定ファイル

### config/thresholds.yaml

```yaml
screening:
  min_volume_usd: 100000        # スクリーニング対象の最低 24h 出来高
  top_n_markets: 50             # 上位何件を詳細チェックするか
  excluded_categories: []       # 除外カテゴリ(例: "Sports")

large_trade:
  min_trade_usd: 50000          # 単一取引の最低額
  min_trade_usd_hourly: 100000  # 1 時間累計の最低額

orderbook_skew:
  skew_ratio: 3.0               # YES/NO 流動性比
  prob_change_1h: 0.05          # 1 時間の確率変動(0.05 = 5%pt)
  min_volume_1h: 20000          # 直近 1 時間の最低出来高

wallet_classification:
  smart_trader:
    min_cumulative_pnl_usd: 100000   # Polymarket 内のオープンポジション PnL
    min_win_rate: 0.60
  whale:
    min_cumulative_volume_usd: 500000
  new:
    max_trade_count: 10

cache:
  wallet_ttl_seconds: 86400     # 24h
```

## 5. 環境変数(.env)

```
DISCORD_WEBHOOK_URL=            # #whale-alerts-poly チャンネル
LOG_LEVEL=INFO
DB_PATH=./data/whale_alerts.db
DRY_RUN=false                   # true なら Discord 送信せず標準出力に表示
POLYMARKET_USER_AGENT=polymarket-whale-alerts/0.1
```

Polymarket 公開 API は認証不要なので API キー設定は無い。

## 6. 実装上の注意

### 6.1 Polymarket API の使い分け

3 つのホストを使い分ける:

- **Gamma** `gamma-api.polymarket.com` — events / markets メタデータ・出来高・確率変化
- **CLOB** `clob.polymarket.com` — トークン単位の板(`/book?token_id=`)
- **Data** `data-api.polymarket.com` — 公開約定フィード(`/trades`)、ウォレット PnL(`/positions`)

レスポンスのフィールド命名は **camelCase**(`volume24hr`, `oneHourPriceChange`, `proxyWallet`)、クエリパラメータと order 値は **snake_case**(`volume_min`, `order=volume_24hr`)で **混在している** 点に注意。

### 6.2 outcomePrices / clobTokenIds のパース

両方とも文字列エンコードされた JSON 配列で返ってくる場合がある:
```
"outcomePrices": "[\"0.6234\",\"0.3766\"]"
"clobTokenIds": "[\"123...\",\"456...\"]"
```
ネイティブ配列で返るパスもあるので両対応する(`_parse_string_array` ヘルパ)。

### 6.3 取引額の計算

Polymarket の `/trades` レスポンスは `size`(シェア数) と `price`(0..1) で返る。USD 額は `size * price`。
`side` は BUY / SELL、`outcome` は Yes / No。表示用には `f"{action} {outcome}"` で結合(例: `BUY YES`)。

### 6.4 重複通知防止

- `transactionHash` を `trade_id` として UNIQUE 制約で防ぐ
- 万一 `transactionHash` が無い場合は `(market_id, wallet_address, amount_usd, detected_at の 1 時間以内)` の組で重複判定

### 6.5 dry-run モード

- `--dry-run` フラグまたは `DRY_RUN=true` で動作
- Polymarket API は通常通り呼ぶ(無料なので問題なし)
- Discord 送信のみスキップし、Embed の JSON を標準ログに出力
- DB への書き込みもスキップ

### 6.6 エラー処理

- API エラー → ログに記録し、当該マーケットだけスキップ(全体停止しない)
- Discord 送信失敗 → 3 回までリトライ(指数バックオフ)、その後はログのみ
- DB エラー → クリティカル(プロセス終了)

### 6.7 レート制限

- 連続呼び出し時は 200ms 以上間隔を空ける(`PolymarketClient._wait_rate_limit`)
- `tenacity` で 5xx / 接続エラーは指数バックオフ最大 3 回リトライ

### 6.8 ロギング

- Python 標準 `logging` を使う
- フォーマット: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- 各実行で「スクリーニング件数」「検知件数」「通知件数」「スキップ件数」をサマリー出力

## 7. テスト

### 単体テスト

- `tests/test_detector.py` — 閾値判定ロジック(モックデータで)
- `tests/test_formatter.py` — Embed 生成
- `tests/test_db.py` — 重複検知・キャッシュ TTL
- `tests/test_polymarket_client.py` — レスポンス正規化(monkeypatch で `_get` を差し替え)
- `tests/test_job.py` — `_process_market` を `FakePolymarket` で通す結合テスト

### 動作確認手順(VPS デプロイ後)

1. `python scripts/run.py --dry-run` で 1 サイクル動かす
2. ログでスクリーニング件数を確認
3. dry-run なしで実行 → Discord に通知が来るか確認
4. cron 登録 → 1 時間放置して継続稼働を確認

## 8. 後続タスク(Phase 1 完了後)

- ヒット率集計スクリプト(`alert_outcomes` を埋めて勝率を出す)
- 閾値の自動チューニング(過去データから適正値を推定)
- Smart Money プロジェクトとの連携(検知ウォレットを Smart Money ウォッチリストの候補にする)
- ウォレット属性判定にクローズ済みポジション(`/closed-positions`)を統合
- Phase 2 で Nansen 等のクロスプロトコル属性を Smart Trader 判定に取り込み検討
