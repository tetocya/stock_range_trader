# 日本株レンジ・平均回帰型バックテスト（Phase 1.1）

## プロジェクトの目的

日本株の日足CSVから一定価格帯を往復する銘柄・期間を検出し、レンジ下側で買って上側で売るLong Onlyの平均回帰戦略を検証するPhase 1システムです。収益率の最大化やパラメータ最適化よりも、将来データを使用せず、現実的な約定順序を再現できるバックテストの正しさ・再現性・テスト可能性を優先します。

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

主な実行時依存はpandas、NumPy、PyYAML、matplotlib、開発用依存はpytestとRuffです。TA-Lib、SciPy、外部Broker SDKには依存しません。

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

Stop Lossは実際のエントリー約定価格に対する当日終値で判定します。Range Breakdownは`Range Score < range_exit_threshold`または`ADX > adx_exit_min`が設定日数連続した場合、Maximum Holdingは保有営業日数が設定値に達した場合に成立します。同日に複数条件が成立した場合は、Stop Loss、Range Breakdown、Maximum Holding、Mean Reversionの順で理由を記録します。

シグナルには判定した営業日だけを記録します。この層では約定せず、後続のExecution Modelが必ず次の営業日の始値を使用します。

## 約定・資産管理

`MarketOnNextOpen`はシグナル日より後の営業日の`MarketBar`だけを受け付けます。`volume > 0`の場合だけ約定し、`volume == 0`ではFillを生成せず`canceled / non_tradable_bar`として当日失効させます。失効注文は次の営業日へ繰り越しません。BUY価格は`open × (1 + slippage_pct)`、SELL価格は`open × (1 - slippage_pct)`です。手数料はスリッページ反映後の約定金額に`commission_rate`を掛けます。証券会社や外部注文先への通信機能はありません。

Position Sizingは`portfolio_value × max_position_pct`を上限とし、手数料込みの利用可能現金も超えない株数を単元株単位で切り下げます。1単元を購入できない場合は注文しません。最大ドローダウン停止に達した場合、新規BUYはバックテスト終了まで停止します。

Trade LogのGross Profitはスリッページ反映済みの売買価格差、Net ProfitはGross Profitから往復手数料を引いた値です。Slippage Costは参考値として別途記録し、Net Profitから二重控除しません。

各BUY/SELLシグナルの注文結果はOrder Logへ保存します。`filled`、`rejected`、`canceled`を区別し、`risk_limit`、`insufficient_capital_for_lot`、`non_tradable_bar`、`no_next_bar`の理由を明示します。Order Logがあるため、「シグナルなし」「リスクまたは資金不足による拒否」「約定不能による失効」「正常約定」を区別できます。

## バックテストの日次処理順

Backtest Engineは日付昇順の各行を次の順序で処理します。

1. 前営業日から保留されたシグナルを当日の始値・出来高で約定判定
2. 現金とポジションを更新
3. 当日終値で時価評価し、保有日数とドローダウンを更新
4. 当日までの特徴量で新しいシグナルを生成
5. シグナルを次に現れる営業日まで保留

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
- Theoretical Buy & Hold：`最終終値 ÷ 初日終値 - 1`の理論的な価格騰落率。資金・単元株・残余現金・コストは反映しません
- Executable Buy & Hold：Strategyと同じ初期資金を使い、初日の始値へBUY側SlippageとCommissionを適用し、設定単元株で買える最大株数を購入します。`max_position_pct`は適用せず、残余現金を保ち、最終終値で時価評価します。強制売却は行わず、1単元も買えない場合は現金100%・リターン0%です
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

Trade Logには`symbol`、`entry_signal_date`、`entry_date`、`entry_price`、`shares`、`exit_signal_date`、`exit_date`、`exit_price`、`exit_reason`、`gross_profit`、`commission`、`slippage_cost`、`net_profit`、`return_pct`、`holding_days`を保存します。

Order Logには`symbol`、`signal_date`、`scheduled_execution_date`、`side`、`requested_shares`、`filled_shares`、`status`、`reason`、`raw_open_price`、`execution_price`、`commission`、`slippage_cost`を保存します。値が確定しない非約定項目は空欄です。

## テスト

```bash
python -m pytest -q
ruff check .
ruff format --check .
```

Unit Testに加え、実サンプルを用いたAPI・CLIのend-to-end test、禁止されたネットワーク／注文送信コードの構造検査、Look-ahead bias専用テストを含みます。

Look-ahead専用テストは、未来データ改変に対する過去結果の不変性、シグナル日と約定日の厳格な前後関係、中央ローリング・未来方向shift・backfillの不使用を検証します。

## 現在の制限事項

- 入力銘柄集合が現時点の上場銘柄だけで作られている場合、Survivorship biasが残ります。上場廃止銘柄を含むpoint-in-time universeはPhase 1では提供しません。
- 株式分割、併合、配当、銘柄コード変更、上場廃止、売買停止の自動処理はありません。入力CSVが目的に適した調整済みデータかは利用者が確認する必要があります。
- Theoretical Buy & Holdは資金制約、単元株、残余現金、配当、手数料、税金を含みません。Executable Buy & Holdは初回BUYの単元株、残余現金、Slippage、Commissionを含みますが、配当・税金・売却費用は含みません。
- 単一銘柄、Long Only、日足、1ポジション、全数量EXITのみです。空売り、信用取引、部分約定、複数銘柄の資金競合は扱いません。
- 約定モデルは翌営業日始値への一定率Slippageで、出来高ゼロだけを非約定として扱います。Bid/Ask spread、板の厚さ、出来高参加率、値幅制限、ストップ高・安、寄付成立時刻、市場インパクトは再現しません。
- Stop Lossは当日終値で判定し、翌営業日始値で約定します。日中に閾値へ到達した瞬間の約定ではありません。
- 最終行のシグナルは翌営業日データがないため約定しません。未決済ポジションは最終終値で時価評価され、完結Trade Logには含まれません。
- 最大ドローダウン停止は、一度発動すると当該バックテスト終了まで新規BUYを再開しない保守的な仕様です。
- パラメータ最適化、Walk-forward、Out-of-sample検証は未実装です。サンプルCSVは人工データであり、戦略の有効性を示しません。
- 機械学習、ニュース・SNS解析、リアルタイムデータ、高頻度・分足・Tick取引はPhase 1の対象外です。
- 実注文機能は存在しません。将来Broker Interfaceを追加する場合も、Paper Tradingと実売買を明示的に分離する必要があります。

## Roadmap

1. **Phase 1.1（現在）**：Phase 1にOrder Log、出来高ゼロ失効、実行可能ベンチマーク、売買代金流動性、ADX互換性、CIを追加
2. **Phase 2**：J-Quants API、東証銘柄一括取得、Range Scoreランキング、複数銘柄Backtest
3. **Phase 3**：Parameter Search、Walk-forward validation、Out-of-sample test
4. **Phase 4**：Paper Trading
5. **Phase 5**：証券会社API連携
6. **Phase 6**：十分な検証とリスク制限を前提とした小規模Live Trading

Phase 2以降でもSurvivorship bias、企業行動、データ時点整合性、実運用コストを個別に検証し、Phase 1の因果的なシグナル・約定境界を維持します。
