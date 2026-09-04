# stock_range_trader

日本株の日足データを対象に、レンジ相場の検出とLong Onlyの平均回帰戦略を検証するバックテストプロジェクトです。Phase 2ではJ-Quants API V2 Freeとyfinance、国内普通株Universe、同一基準日のRange Scoreランキング、銘柄別バックテスト集計を実装しています。Pythonプロジェクト本体は [`stock_range_trader/`](stock_range_trader/) にあります。設計、調整規約、データ制約の詳細は[プロジェクトREADME](stock_range_trader/README.md)を参照してください。

> 本システムは調査・バックテスト専用です。実注文機能や投資助言機能はありません。

## Installation

Python 3.11以上を使用します。

```bash
git clone https://github.com/tetocya/stock_range_trader.git
cd stock_range_trader/stock_range_trader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## テストと品質チェック

Pythonプロジェクトのディレクトリで実行します。

```bash
cd stock_range_trader
python -m pytest -q
ruff check .
ruff format --check .
```

## サンプル実行

同梱の人工データとデフォルト設定で単一銘柄バックテストを実行できます。

```bash
cd stock_range_trader
python examples/run_single_stock.py \
  --data data/sample.csv \
  --symbol 7203 \
  --config config/strategy.yaml \
  --output-dir outputs
```

結果は `outputs/` にTrade Log、Order Log、Equity Curve、およびPNGグラフとして保存されます。

## Phase 2クイックスタート

J-QuantsのAPIキーは環境変数`JQUANTS_API_KEY`だけから読み込みます。`.env.example`に実キーは含まれません。

```bash
cd stock_range_trader
export JQUANTS_API_KEY="<your-api-key>"
python examples/download_universe.py --provider jquants --as-of YYYY-MM-DD
python examples/download_prices.py --provider yfinance --years 5
python examples/run_screening.py --provider yfinance --as-of YYYY-MM-DD --top 30
python examples/run_batch_backtest.py \
  --provider yfinance \
  --ranking outputs/range_ranking_YYYY-MM-DD.csv
```

J-Quantsとyfinanceの価格は連結しません。キャッシュとRaw DataはGit管理対象外です。結果にはSurvivorship bias、Provider間差、現在Universeを使う探索的・in-sample設計の制限があり、利益を保証しません。
