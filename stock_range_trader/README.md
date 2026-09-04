# 日本株レンジ・平均回帰型バックテスト（Phase 2.1）

## プロジェクトの目的

日本株の中から一定価格帯を往復する銘柄を検出し、レンジ下側で買って上側で売るLong Onlyの平均回帰戦略を検証します。Phase 2.1ではPhase 2のデータパイプラインを維持しつつ、Signal PriceとExecution Priceの分離、企業行動の会計、J-Quantsの実HTTP retry制御、Range Score時系列評価CLIを追加します。

> **重要:** 本システムは調査・バックテスト専用です。証券会社API、実注文、ペーパートレード、投資助言機能はありません。出力は将来の運用成績を保証しません。

Phase 1で提供する機能は次のとおりです。

- 厳格なOHLCV CSV読込・検証
- SMA、Wilder ATR、TA-Lib互換の初期化規約によるWilder ADX
- 複数要素によるRange Score
- ATRベースのLong Only平均回帰戦略
- 翌営業日始値約定、出来高ゼロBarでの失効、スリッページ、手数料、単元株
- Stop Loss、Range Breakdown、Maximum Holding、最大DD停止
- Trade Log、Order Log、Equity Curve、Performance Metrics、PNGグラフ
- Look-ahead bias専用回帰テスト

## Installation

Python 3.11以上を使用します。プロジェクト専用の仮想環境を推奨します。

```bash
cd stock_range_trader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

`requirements.txt`を使用する場合は、`python -m pip install -r requirements.txt`でも依存ライブラリを導入できます。ただし、`examples/run_single_stock.py`を直接実行する場合はeditable installも行ってください。

主な実行時依存はpandas、NumPy、PyYAML、matplotlib、pyarrow、`jquants-api-client`、yfinance、開発用依存はpytestとRuffです。TA-Lib、SciPy、外部Broker SDKには依存しません。

## 入力データ形式

UTF-8 CSVで、次の列名を大文字・小文字も含めて使用します。追加列は保持されます。

| 列 | 内容 | 条件 |
|---|---|---|
| `date` | 営業日 | 日付として解釈可能、重複なし、昇順 |
| `open` | 始値 | 正の有限数 |
| `high` | 高値 | 正の有限数、Open・Close・Low以上 |
| `low` | 安値 | 正の有限数、Open・Close・High以下 |
| `close` | 終値 | 正の有限数 |
| `volume` | 出来高 | 非負の有限数。0は入力可能だが約定不能 |

```csv
date,open,high,low,close,volume
2025-01-06,1000,1020,990,1010,150000
2025-01-07,1015,1030,1005,1025,180000
```

欠損、非数値、無限値、不正なOHLC関係、重複日付、降順・非単調な日付は例外になります。Loaderは日付の並べ替え、欠損補完、価格修正を黙って行いません。

## アーキテクチャ

```text
CSV → Loader / Validation
    → SMA / ATR / ADX
    → RangeDetector → RangeScorer
    → MeanReversionStrategy
    → BacktestEngine
         ├─ RiskManager
         ├─ MarketOnNextOpen
         └─ Portfolio
    → Metrics → Console / Trade・Order・Equity CSV / PNG
```

各層はDataFrameまたは明示的なdataclassで接続されます。Strategyはシグナルだけを生成し、Execution Modelは当日のOHLCVを表す`MarketBar`を受け取ってローカルの約定シミュレーションだけを行います。

```text
data/        CSV読込・検証
indicators/  SMA・ATR・ADX
screening/   レンジ特徴量・Range Score
strategy/    戦略Interface・売買条件
backtest/    Engine・Execution・Portfolio・Trade・Order Log
risk/        Position Sizing・リスク制約
metrics/     Performance Metrics
reports/     コンソール・CSV・PNG
config/      YAML設定と型付き設定Loader
examples/    単一銘柄CLI
tests/       Unit・統合・先読み・安全性テスト
```

## Configuration

すべての主要パラメータは`config/strategy.yaml`で管理します。指標期間、ATR倍率、Range Scoreの重みと閾値、EXIT条件、資金・単元株、スリッページ、手数料、年率換算日数を変更できます。

設定Loaderは必須キーの欠落と未知のキーを拒否します。Range Scoreの重みは非負かつ合計1.0でなければならず、自動補正しません。

## 戦略概要

20日SMAを中心としたATR Envelopeを売買水準とし、Range ScoreとADXでレンジ状態を確認してから新規BUYを許可します。ポジション保有中は平均回帰、損切り、レンジ崩壊、最大保有期間のいずれかでEXITします。空売り、信用取引、複数銘柄の同時運用は行いません。

## 指標の計算規約

- SMAは当日を含む末尾方向の単純移動平均で、部分期間の値は出力しません。
- ATRはデフォルトでWilder方式を使用します。最初の値をTrue Rangeの単純平均で初期化し、以降をWilderの再帰式で平滑化します。設定により単純移動平均方式も選択できます。
- ADXのTR、方向性移動、DXはWilder方式で平滑化します。最初のBarは前日値がないため方向性移動の観測期間に含めません。unstable periodを0とする[TA-Lib公式`TA_ADX`実装](https://github.com/TA-Lib/ta-lib/blob/main/src/ta_func/ta_ADX.c)と同じlookback規約を採用し、最初のADXは0-based indexの`2 × period - 1`（`period=14`ではindex 27）に出力します。TA-Libは実行時・テスト時依存ではありません。定数価格系列ではindex 27以降を0とします。すべての計算は当日以前のデータだけを使用します。

## Range Scoreの計算規約

Range Scoreは0～100点で、初期重みはtrend 30%、mean reversion 30%、stability 20%、liquidity 20%です。各日について、その日までの末尾ローリング値だけから次の部分点を計算します。

- `trend_score`：正規化SMA傾斜の絶対値とADXを、それぞれ設定上限で0点となる線形スケールへ変換します。両者が小さいほどレンジ的であるため高得点です。
- `mean_reversion_score`：終値がSMAを横切った回数を目標回数まで線形評価します。SMAと同値の日は直前の側を維持し、単なる接触を往復2回として数えません。
- `stability_score`：価格水準をまたいで比較できるよう、`ATR / SMA`とレンジ幅の変動係数を評価します。両者の変動が小さいほど高得点です。
- `liquidity_score`：各日の`close × volume`を売買代金とし、末尾`liquidity_window`期間の中央値を`median_trading_value_target`（初期値は1日1億円）まで線形評価します。中央値により一時的な出来高急増の影響を抑え、異なる価格水準の銘柄を平均出来高より整合的に比較します。

Phase 1のCSVでは売買代金を`close × volume`で計算します。Phase 2で調整株価と非調整株価を併用する際は、出来高との組み合わせが同じ経済的売買代金を表すかを再検討する必要があります。

すべての部分点を0～100へ制限した後に重み付けします。重みが非負でない場合や合計が1でない場合は、自動補正せず設定エラーとします。

## 売買シグナル

戦略はLong Onlyです。当日終値が`SMA - buy_atr_multiplier × ATR`以下で、Range ScoreとADXのエントリーフィルターを満たした場合にBUYシグナルを生成します。保有中は、終値が`SMA + sell_atr_multiplier × ATR`以上へ回帰した場合にSELLシグナルを生成します。

Stop Lossはエントリー時の約定をSignal Priceと同じ調整尺度へ写した価格と、当日のSignal Closeを比較します。現金会計に使う非調整約定価格と尺度を混合しません。Range Breakdownは`Range Score < range_exit_threshold`または`ADX > adx_exit_min`が設定日数連続した場合、Maximum Holdingは保有営業日数が設定値に達した場合に成立します。同日に複数条件が成立した場合は、Stop Loss、Range Breakdown、Maximum Holding、Mean Reversionの順で理由を記録します。

シグナルには判定した営業日だけを記録します。この層では約定せず、後続のExecution Modelが必ず次の営業日の始値を使用します。

## 約定・資産管理

`MarketOnNextOpen`はシグナル日より後の営業日の`MarketBar`だけを受け付けます。`volume > 0`の場合だけ約定し、`volume == 0`ではFillを生成せず`canceled / non_tradable_bar`として当日失効させます。失効注文は次の営業日へ繰り越しません。BUY価格は`open × (1 + slippage_pct)`、SELL価格は`open × (1 - slippage_pct)`です。手数料はスリッページ反映後の約定金額に`commission_rate`を掛けます。証券会社や外部注文先への通信機能はありません。

Position Sizingは`portfolio_value × max_position_pct`を上限とし、手数料込みの利用可能現金も超えない株数を単元株単位で切り下げます。1単元を購入できない場合は注文しません。最大ドローダウン停止に達した場合、新規BUYはバックテスト終了まで停止します。

Trade LogのGross Profitはスリッページ反映済みの売買価格差、Net ProfitはGross Profitから往復手数料を引いた値です。Slippage Costは参考値として別途記録し、Net Profitから二重控除しません。

各BUY/SELLシグナルの注文結果はOrder Logへ保存します。`filled`、`rejected`、`canceled`を区別し、`risk_limit`、`insufficient_capital_for_lot`、`non_tradable_bar`、`no_next_bar`の理由を明示します。Order Logがあるため、「シグナルなし」「リスクまたは資金不足による拒否」「約定不能による失効」「正常約定」を区別できます。

## バックテストの日次処理順

Backtest Engineは日付昇順の各行を次の順序で処理します。

1. 当日寄付前に有効な株式分割があれば保有株数と取得原価を同時に調整
2. 前営業日から保留されたシグナルを当日の非調整始値・非調整出来高で約定判定
3. 非調整約定価格で現金とポジションを更新
4. 当日の非調整終値で時価評価し、保有日数とドローダウンを更新
5. 当日までの調整済み特徴量で新しいシグナルを生成
6. シグナルを次に現れる営業日まで保留

最終行で生じたシグナルには利用可能な翌営業日始値がないため、架空の価格で約定せず`canceled / no_next_bar`として記録します。未決済ポジションは最終終値で時価評価し、Trade Logには完結した取引だけを記録します。Equity Curveは日次の`date`、`cash`、`position_value`、`total_equity`、`drawdown`を保持します。

## Performance Metrics

- Total Return：`最終時価資産 / 初期資金 - 1`
- CAGR：Equity Curveのリターン区間数を設定された年間営業日数で年換算
- Win Rate：Net Profitが正の完結取引数を全完結取引数で除算
- Average Profit / Winning / Losing Trade：Net Profitの全体・勝ち・負け別平均
- Profit Factor：正のNet Profit合計を負のNet Profit合計の絶対値で除算。利益があり損失がない場合は無限大、取引がない場合は未定義
- Maximum Drawdown：日次時価資産を過去最高値で除したドローダウンの最小値
- Sharpe Ratio：日次超過リターンの平均を標本標準偏差で除し、年間営業日数の平方根で年率換算
- Sortino Ratio：日次超過リターンの平均を負の超過リターンの二乗平均平方根で除し、年率換算
- Average Holding Period：完結取引の保有営業日数の平均
- Exposure：終値時点のPosition Valueが正である営業日の割合
- Theoretical Buy & Hold：初日と最終日の非調整終値を使い、期間内の明示的な分割比率だけを株数へ反映した理論価格リターン。資金・単元株・残余現金・コスト・配当は反映しません
- Executable Buy & Hold：Strategyと同じ初期資金を使い、初日の非調整始値へBUY側SlippageとCommissionを適用し、設定単元株で買える最大株数を購入します。明示的な分割比率で保有株数を変更し、非調整終値で時価評価します。`max_position_pct`、配当、税金、強制売却は含みません
- Strategy vs Executable Buy & Hold：Total ReturnからExecutable Buy & Hold Returnを引いた比較可能な差

旧APIの`buy_and_hold_return`はTheoretical Buy & Holdを意味する非推奨aliasです。新規コードでは曖昧さのない各フィールドを使用してください。Equity Curve比較グラフはExecutable Buy & Holdを表示します。

## Look-ahead bias対策

指標、Range Score、戦略条件は当日以前の末尾ローリングデータだけで計算します。`center=True`、未来方向の`shift`、`bfill`は使用しません。当日の終値で生成したシグナルは保留され、次に存在する営業日の始値でのみ約定できます。

専用回帰テストでは、80日目以降のopen、high、low、close、volumeを極端に変更しても1～79日目の指標、Range Score、Signal Log、Order Log、Fill、Trade Logが変わらないことを確認します。また、終値シグナルが同日の始値を利用できないことと、計算モジュール内に中央ローリング・未来方向shift・backfillがないことを検査します。

## 実行方法とEnd-to-endサンプル

`data/sample.csv`は機能確認専用に生成した320営業日分の人工的な往復価格です。実在銘柄の価格や期待収益を表すものではありません。

```bash
python examples/run_single_stock.py \
  --data data/sample.csv \
  --symbol 7203 \
  --config config/strategy.yaml \
  --output-dir outputs
```

実行すると`trade_log.csv`、`order_log.csv`、`equity_curve.csv`、`price_chart.png`、`equity_curve.png`、`drawdown.png`を出力します。コンソールにはStrategy Return、Theoretical / Executable Buy & Hold、両者との差、およびFilled / Rejected / Canceled Ordersを表示します。

デフォルトの出力内容は次のとおりです。

| ファイル | 内容 |
|---|---|
| `trade_log.csv` | 完結した全取引と損益・コスト・EXIT理由 |
| `order_log.csv` | 全注文結果のstatus、reason、要求・約定株数、価格・コスト |
| `equity_curve.csv` | 日次Cash、Position Value、Total Equity、Drawdown |
| `price_chart.png` | Close、SMA、BUY/SELL水準、実約定位置 |
| `equity_curve.png` | StrategyとExecutable Buy & Holdの資産推移 |
| `drawdown.png` | Strategyの日次Drawdown |

Trade Logには`symbol`、`entry_signal_date`、`entry_date`、`entry_price`、`shares`、`split_adjustment_ratio`、`split_adjusted_entry_price`、`exit_shares`、`exit_signal_date`、`exit_date`、`exit_price`、`exit_reason`、`gross_profit`、`commission`、`slippage_cost`、`net_profit`、`return_pct`、`holding_days`を保存します。

Order Logには`symbol`、`signal_date`、`scheduled_execution_date`、`side`、`requested_shares`、`filled_shares`、`status`、`reason`、`raw_open_price`、`execution_price`、`commission`、`slippage_cost`を保存します。値が確定しない非約定項目は空欄です。

## Phase 2のデータ取得設計

Phase 2は「1回の実行につき1つのPrice Provider」を不変条件とします。Providerを連結しません。つまり、古い期間のyfinanceと新しい期間のJ-Quantsを連結したOHLCVは作りません。調整規約、取得可能期間、欠損、価格単位の違いを1本の時系列の中に隠さないためです。Strategy、RangeScorer、BacktestEngineは外部APIを直接呼び出さず、Providerが次のCanonical Schemaへ変換したデータだけを受け取ります。

```text
date, symbol, provider,
raw_open, raw_high, raw_low, raw_close, raw_volume, turnover_value,
adjusted_open, adjusted_high, adjusted_low, adjusted_close, adjusted_volume,
adjustment_factor, dividend, stock_split, fetched_at
```

Phase 1.1 AdapterはCanonicalデータを下記の2本の価格laneへ分けます。Liquidity ScoreにはProvider側の実売買代金を優先します。すべてのPhase 2派生出力に`provider`、`requested_start`、`requested_end`、`actual_start`、`actual_end`、`adjustment_mode`、`universe_as_of_date`に加え、Signal・Execution・企業行動・配当・2種類のBenchmarkの価格規約を付けます。日付区間は開始日を含み、`end`を含まない半開区間です。特にyfinanceの`end`は排他的であることをテストで固定しています。

### Signal PriceとExecution Price

| 用途 | 使用価格 | 使用箇所 |
|---|---|---|
| Signal lane | Providerの調整済みOHLCV | SMA・ATR・ADX、Range特徴量、Range Score、エントリー／イグジットシグナル、Stop Loss判定 |
| Execution lane | 当時の非調整OHLCV | 翌営業日始値約定、Position Sizing、Commission、Slippage Cost、Cash、終値時価評価、Benchmark |

`open/high/low/close/volume`は後方互換のSignal laneです。`signal_*`と`execution_*`を別列とし、後者を実約定価格として扱います。調整済み価格をFill、購入可能株数、手数料、現金残高に使用しません。当日終値のSignalは引き続き次に現れる営業日のExecution Openでのみ約定します。

配当はCanonicalに保持しますが、StrategyとTheoretical／Executable Buy & Holdのいずれの利益・現金にも加算しない「配当除外の価格リターン」規約です。YahooのSignal laneは`Adj Close / Close`由来のため配当調整の影響を含み得ますが、これは指標の尺度調整であり、配当金を利益とする意味ではありません。

株式分割はyfinanceの明示的な`Stock Splits`を効力発生日の寄付前に既存ポジションへ適用し、株数に比率を掛け、一株当たり取得原価を同比率で割ります。現金は動かしません。端数株が発生しCash-in-lieuが必要な場合は`UnsupportedCorporateActionError`で停止します。J-Quantsの`AdjFactor`はYahooの分割イベント比率と同一とみなせないため、選択期間に1以外がある場合は不正確なExecutable結果を出さず同例外で明示的に停止します。

### J-Quants API V2 Free

[公式Pythonクライアント](https://github.com/J-Quants/jquants-api-client-python)の`ClientV2`トランスポートを使い、上場銘柄マスタ、日足、取引カレンダーのV2 endpointを扱います。Freeは5年分の価格取得に使わず、12週間遅延した利用可能期間でUniverse、公式価格との重複期間比較、J-Quants単独の短期実行に使います。取得可能日を固定日付で仮定せず、実際の最古日・最新日をmanifestに保存します。

`config/data_sources.yaml`は`plan: free`、`rate_limit_per_minute: 5`、`min_request_interval_seconds: 13`を固定します。ページネーションも含めて全リクエストを直列実行し、公式クライアントの並列`*_range`に依存しません。公式`ClientV2`がSessionに設定するurllib3 Retryは`total/connect/read/redirect/status/other=0`のAdapterで無効化し、すべての実HTTP試行を外側の逐次Rate Limiter経由にします。外側のみが429、5xx、network errorをretryし、429は`Retry-After`を優先、それ以外は指数バックオフ＋jitterを使います。`timeout_seconds`は各`Session.get(..., timeout=...)`へ実際に渡します。

この制御はテスト済みの`jquants-api-client 2.6.x`系と、そのprivate APIである`_request_session`、`_base_headers`、`_raise_for_status`、`JQUANTS_API_BASE`に限定的に依存します。必要メンバが消失した版は初期化時に失敗し、内部retryに黙って戻りません。公式クライアント更新時は実際の`ClientV2` Session Adapterを調べる回帰テストの確認が必要です。Free契約でカレンダーendpointを使えない場合、平日を代用することはせず、upstreamのエラーを明示します。

APIキーは環境変数`JQUANTS_API_KEY`以外から読み込みません。CLI引数、YAML、ログへ渡す設計はありません。

```bash
export JQUANTS_API_KEY="<your-api-key>"
```

リポジトリ直下の`.env.example`は空の変数名だけを示し、`.env`はGit対象外です。

J-Quantsは`AdjO / AdjH / AdjL / AdjC / AdjVo`を調整済みOHLCV、`Va`を売買代金としてそのまま保持します。`AdjFactor`は調整係数として保持しますが、Yahooの分割イベント比率と同じとは見なしません。日足レスポンスにない配当金や分割イベントを推測して補いません。

### yfinanceの5年価格

yfinanceは個人の研究・バックテスト用に限定し、直近5年の日足取得に使います。[公式`download`リファレンス](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)に従い、取得引数は`interval="1d"`、`auto_adjust=False`、`actions=True`、`keepna=True`、`progress=False`、`threads=False`を明示します。50銘柄ずつのbatchに分割し、空応答、不明ticker、部分失敗は銘柄別のstatusと理由に記録します。

Raw Open/High/Low/Close/VolumeとDividends/Stock Splitsを保持し、`Adj Close / Close`比をOHLCにだけ適用します。この比率には配当の影響が含まれ得るため、Volumeに逆数を掛けて分割調整量と見なすことはしません。`adjusted_volume`はRaw Volume、`turnover_value`はRaw Close × Raw Volumeです。J-Quantsとの調整規約差は自動補正せずProvider比較に残します。

## 国内普通株Universe

J-Quants上場銘柄マスタの公式コードだけで判定します。商品区分`011`で、市場区分`0111`（Prime）、`0112`（Standard）、`0113`（Growth）の銘柄を含めます。ETF、ETN、REIT、インフラファンド、優先株、出資証券、外国株・外国ETF、PRO Marketなどは商品・市場コードが対象外のため除外されます。名称の部分一致は使いません。

Universe Snapshotは除外銘柄も含め、基準日、J-Quantsコード、会社名、市場、業種、商品区分、Yahoo ticker、判定、除外理由を保存します。J-Quantsの5桁コードは常に文字列で保持し、`72030`→`7203.T`、`130A0`→`130A.T`の末尾規則を正規表現で検証します。推測で変換できない銘柄は`unresolved_symbols.csv`へ出力します。

Snapshotのマスタ日と`as_of_date`の一致を検証し、現在Universeを過去Universeとしてラベル付けしません。一方、Freeとyfinanceだけで5年前の完全なpoint-in-time Universeは再現できない可能性があります。現在Universeを使った5年分の結果は「探索的・in-sample記述」であり、Survivorship bias除去済み、または予測性能の検証とは表現しません。

## キャッシュとmanifest

Provider別の本体を`.data_cache/<provider>/<dataset>/`のParquet、要求別manifestを`.data_cache/manifests/`のJSONで保存します。要求内容のSHA-256 keyが一致する場合はAPIを呼ばず再利用し、`--refresh`の場合だけ再取得します。Parquetを先、それを指すmanifestを後に原子的に公開するため、途中失敗時も完了済みキャッシュは維持されます。

manifestにはProvider、endpoint/method、symbols、要求期間、実際の取得期間、UTC取得時刻、Schema version、調整モード、library version、行数、列、content hash、Universe基準日、status集計、注記を保存します。読込時にhash、Schema、行数、列、要求メタデータを再検証します。`.data_cache/`、`*.parquet`、`data/raw/`はGit対象外であり、J-QuantsやYahooのRaw DataをGitHubへ掲載してはいけません。

## Phase 2 CLI

コマンドはPythonプロジェク直下で実行します。先に基準日Universeを作成し、同じSnapshotで価格、スクリーニング、独立バックテストを実行します。

```bash
python examples/download_universe.py \
  --provider jquants \
  --as-of YYYY-MM-DD

python examples/download_prices.py \
  --provider yfinance \
  --years 5

# J-Quantsは小さく開始（Providerの5桁コード）
python examples/download_prices.py \
  --provider jquants \
  --symbols 72030,99840 \
  --years 1

python examples/run_screening.py \
  --provider yfinance \
  --as-of YYYY-MM-DD \
  --top 30

python examples/run_batch_backtest.py \
  --provider yfinance \
  --ranking outputs/range_ranking_YYYY-MM-DD.csv

python examples/compare_providers.py \
  --symbols 1301,7203,8306,9984

python examples/evaluate_range_score.py \
  --input outputs/yfinance_prices.parquet \
  --provider yfinance \
  --forward-sessions 20 \
  --output-dir outputs/range_score_evaluation
```

`download_prices.py --end`も排他的です。例えば2026-09-01まで指定した場合、対象は2026-08-31までです。`--refresh`を付けない同一要求はキャッシュを再利用します。APIキーをCLI引数で渡すオプションはありません。

J-Quantsのキャッシュmiss時は、APIを呼ぶ前に対象銘柄数、1銘柄1page以上とする最小推定request数、`request数 × 13秒`の保守的な最低所要時間を表示します。paginationとretryがあれば実時間は増えます。例えば4,000銘柄なら最少4,000 request、52,000秒（約14時26分40秒）です。未絞り込みのUniverse全件は`--allow-long-run`を付けない限り開始しません。通常は`--symbols`または`--limit`で小さく確認します。

Screeningは同一`as_of_date`の観測を持つ銘柄だけをRange Score降順、同点はJ-Quantsコード昇順で決定論的に並べます。取得失敗、空応答、履歴・Warm-up不足、基準日の欠測、不正OHLCVは`screening_exclusions_<date>.csv`に理由を残します。上位初期値は30銘柄で、利益率はランキングに使いません。

Batch Backtestは各銘柄でPhase 1.1 Engineを新規生成し、各々の初期資金で独立実行します。共通資金のPortfolioではありません。`batch_backtest_summary.csv`、`batch_trade_log.csv`、`batch_order_log.csv`を出力し、1銘柄の失敗で全体を停止させません。

Provider比較は重複日のRaw OHLC、Volume、Adjusted Close、Turnoverの相対差を計算し、Provider間差異が許容幅を超える列をWarningにします。J-Quantsを公式基準としますが、どちらのデータも書き換えず、5年yfinance時系列の一部をJ-Quantsで置換しません。

Range Scoreの時系列評価は各月末にデータをその日付まで切り、Range DetectorとScorerを再計算します。0–40、40–60、60–80、80–100の固定Binごとに銘柄数と観測数、Forward Returnの平均・中央値、評価日SMAへの平均回帰到達率、Win Rate、平均MAE/MFE、Maximum Drawdown、Forward Returnの標準誤差と95%正規近似信頼区間を出力します。売買規則を適用しない観測評価のためProfit FactorはN/Aです。閾値や戦略パラメータの最適化は行いません。

`range_score_observations.csv`、`range_score_bin_summary.csv`、`range_score_evaluation_manifest.json`を出力します。入力にUniverse Snapshotを要求しないCLIのため、manifestの`universe_as_of_date`は`null`とし、供給された銘柄集合にSurvivorship biasが残り得ると記録します。月次観測のForward期間は隣接月と重複し得るため、観測数は独立サンプル数ではありません。見かけ上のサンプル数増加と信頼区間の過小評価に注意が必要です。

## Phase 2のデータ品質と制限

- J-Quants Freeの遅延と期間制限により、希望する5年価格はyfinanceに依存します。取得可能期間はAPI応答とmanifestで確認が必要です。
- Yahooの非公式性、tickerの対応関係、配当・分割・銘柄コード変更、通貨・価格単位、売買停止、上場廃止は別途確認が必要です。欠損価格を自動補間しません。
- J-QuantsとYahooは調整規約や日付、出来高、売買代金が完全一致するとは限りません。J-Quantsの`AdjFactor != 1`を分割イベントとして完全再現できない期間はExecutableバックテストをUnsupportedとします。比較Warningは品質調査の入口であり、正しい値の自動決定ではありません。
- 価格がある日だけでカレンダーを完全再現することはできません。Freeで公式カレンダーを取得できない実行では、長期欠損率の判定に利用できません。
- 5年前の上場廃止銘柄を含む完全なpoint-in-time UniverseがなければSurvivorship biasが残ります。現在のRange Scoreで銘柄を選び過去5年を表示しても予測性能の検証にはなりません。
- Phase 2はパラメータ最適化、Walk-forward、Out-of-sample、機械学習、分足・Tick、共通資金Portfolio、Paper Trading、Broker API、実注文を実装しません。
- Phase 2結果はデータと仮定に基づく探索的バックテストであり、利益や将来の成績を保証しません。

## テスト

```bash
python -m pytest -q
ruff check .
ruff format --check .
```

Unit Testに加え、Phase 1.1の実サンプルCLI、Phase 2の固定fixtureとmockによるdownload→cache→screening→batch backtest→reportのend-to-end test、禁止されたネットワーク／注文送信コードの構造検査、Look-ahead bias専用テストを含みます。通常のpytestは外部APIを呼びません。Live Testは`RUN_LIVE_JQUANTS_TESTS=1`または`RUN_LIVE_YFINANCE_TESTS=1`を明示した場合だけ最小範囲で実行され、J-Quantsはさらに`JQUANTS_API_KEY`がなければskipされます。

Look-ahead専用テストは、未来データ改変に対する過去結果の不変性、シグナル日と約定日の厳格な前後関係、中央ローリング・未来方向shift・backfillの不使用を検証します。

## 現在の制限事項

- 入力銘柄集合が現時点の上場銘柄だけで作られている場合、Survivorship biasが残ります。上場廃止銘柄を含むpoint-in-time universeはPhase 1では提供しません。
- yfinanceの明示的な株式分割・併合は株数と取得原価に反映しますが、端数株のCash-in-lieu、配当再投資、配当税、銘柄コード変更、上場廃止、売買停止は再現しません。必要な企業行動を明示的に再現できない場合は不正確なExecutable結果を出力しません。
- Theoretical Buy & Holdは資金制約、単元株、残余現金、配当、手数料、税金を含みません。Executable Buy & Holdは初回BUYの単元株、残余現金、Slippage、Commission、明示的な株式分割を含みますが、配当・税金・売却費用は含みません。
- 単一銘柄、Long Only、日足、1ポジション、全数量EXITのみです。空売り、信用取引、部分約定、複数銘柄の資金競合は扱いません。
- 約定モデルは翌営業日始値への一定率Slippageで、出来高ゼロだけを非約定として扱います。Bid/Ask spread、板の厚さ、出来高参加率、値幅制限、ストップ高・安、寄付成立時刻、市場インパクトは再現しません。
- Stop Lossは当日終値で判定し、翌営業日始値で約定します。日中に閾値へ到達した瞬間の約定ではありません。
- 最終行のシグナルは翌営業日データがないため約定しません。未決済ポジションは最終終値で時価評価され、完結Trade Logには含まれません。
- 最大ドローダウン停止は、一度発動すると当該バックテスト終了まで新規BUYを再開しない保守的な仕様です。
- パラメータ最適化、Walk-forward、Out-of-sample検証は未実装です。サンプルCSVは人工データであり、戦略の有効性を示しません。
- 機械学習、ニュース・SNS解析、リアルタイムデータ、高頻度・分足・Tick取引はPhase 1の対象外です。
- 実注文機能は存在しません。将来Broker Interfaceを追加する場合も、Paper Tradingと実売買を明示的に分離する必要があります。

## Roadmap

1. **Phase 1.1（完了）**：Phase 1にOrder Log、出来高ゼロ失効、実行可能ベンチマーク、売買代金流動性、ADX互換性、CIを追加
2. **Phase 2（完了）**：J-Quants API V2 Free、yfinance、国内普通株Universe、Provider別cache、Range Scoreランキング、銘柄別Backtest集計、Provider間比較
3. **Phase 2.1（現在）**：Signal/Execution Price分離、分割会計、J-Quants実HTTP Rate Limit、Range Score固定Bin評価
4. **Phase 3**：Parameter Search、Walk-forward validation、Out-of-sample test
5. **Phase 4**：Paper Trading
6. **Phase 5**：証券会社API連携
7. **Phase 6**：十分な検証とリスク制限を前提とした小規模Live Trading

Phase 2以降でもSurvivorship bias、企業行動、データ時点整合性、実運用コストを個別に検証し、Phase 1の因果的なシグナル・約定境界を維持します。
