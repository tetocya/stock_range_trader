# stock_range_trader

日本株の日足データを対象に、レンジ相場の検出とLong Onlyの平均回帰戦略を検証するバックテストプロジェクトです。Pythonプロジェクト本体は [`stock_range_trader/`](stock_range_trader/) にあります。設計、計算規約、制約の詳細は[プロジェクトREADME](stock_range_trader/README.md)を参照してください。

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
