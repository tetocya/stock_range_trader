# stock_range_trader

日本株の日足データを対象に、レンジ相場の検出とLong Onlyの平均回帰戦略を検証する研究用プロジェクトです。Phase 3 Walk-forward Validationまで実装済みで、調整済み価格上のSignal Validationと、検証済みExecution価格上のExecutable Validationを別の分析modeとして扱います。

yfinanceはスクリーニング／Signal Validation専用です。yfinanceによるExecutable ValidationとBenchmarkは常に`unsupported`として拒否されます。Executable Validationは、現在は検証済み価格契約を持つJ-Quants入力だけに限定されます。Pythonプロジェクト本体は[`stock_range_trader/`](stock_range_trader/)にあります。

> 本システムは調査・バックテスト専用です。実注文機能ではなく、投資助言や利益保証も行いません。

## Phase 3の公開契約

- [プロジェクトREADME](stock_range_trader/README.md)：価格、Fold、Purge、Candidate選択、CLI、制限事項
- [Phase 3 Manifest仕様](stock_range_trader/docs/phase3_manifest_spec.md)：`walk_forward_manifest.json`と12／14ファイルbundleの規範

詳細仕様は上記文書に集約し、このREADMEでは重複しません。

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
python examples/evaluate_range_score.py \
  --input outputs/yfinance_prices.parquet \
  --provider yfinance \
  --output-dir outputs/range_score_evaluation
```

yfinanceのランキングを`run_batch_backtest.py`へ渡しても銘柄別statusは常に`unsupported`となり、損益は出力されません。Executable検証には価格basisを確認できるProviderの同一Providerデータとランキングが必要です。

J-Quantsとyfinanceの価格は連結しません。J-Quants全Universe価格取得は推定request数と最低時間を事前表示し、`--allow-long-run`の明示的な指定がなければ開始しません。キャッシュとRaw DataはGit管理対象外です。結果にはSurvivorship bias、Provider間差、現在Universeを使う探索的・in-sample設計の制限があり、利益を保証しません。
