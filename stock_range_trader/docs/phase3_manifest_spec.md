# Phase 3 `walk_forward_manifest.json`仕様

この文書は[プロジェクトREADME](../README.md)で説明するPhase 3 Walk-forward Validationの完成Manifestについて、現在のProduction実装を規範化したものです。将来予定のfieldは含みません。

## 適用範囲

| 項目 | 契約 |
|---|---|
| 対象ファイル | `walk_forward_manifest.json` |
| Schema version | `phase3-report-1.0` |
| `status` | `completed` |
| 対応mode | `signal_validation`、`executable_validation` |
| Encoding | UTF-8 |
| JSON形式 | key sort済み、2-space indent、末尾newline |
| 数値 | NaN／Infinity禁止 |

Manifestは完成bundleの最後に書き込まれます。Manifest自身は自己hashせず、`artifacts`にも含めません。Report Builder／Writerは完成済みの型付き結果だけを受け取り、Strategy、Indicator pipeline、Evaluator、Candidate Selector、Backtest Engine、Test評価を再実行しません。

## Top-level section

現在のManifestは次のtop-level keyをすべて必須とします。

| Key | 用途 |
|---|---|
| `schema_version` | Report Schemaの互換性識別子 |
| `status` | 完成bundleを示す`completed` |
| `experiment` | Experiment ID、analysis mode、OOS判定 |
| `source` | Git sourceと再現性状態 |
| `runtime` | 実行時刻、OS、Python、package version |
| `inputs` | Canonical入力の範囲、schema、hash、行・列 |
| `universe` | Universe identity、coverage、bias判定 |
| `provider_capability` | Providerとmodeの許可契約 |
| `price_policy` | Signal／Execution／配当／企業行動／Benchmark規約 |
| `configuration` | 正規化設定、Candidate Catalog、設定file hash |
| `fold_schedule` | configured境界、fold、embargo、forward horizon |
| `selection_policy` | mode固有のeligible条件と順位 |
| `test_policy` | Validation-only選択と一回Testの固定規則 |
| `results` | mode固有のTest-only OOS集約 |
| `exclusions` | 除外集計、銘柄数、詳細CSV |
| `artifacts` | Manifest以外の成果物のhashと固定CSV schema |
| `lineage` | 派生元Experimentと変更理由 |
| `limitations` | 常に明示する分析上の制約 |
| `warnings` | run固有のOOS／Universe警告 |

JSON objectのkey順に意味はありません。Consumerは表示順や辞書順に依存してはいけません。

## `experiment`

| Field | 意味 |
|---|---|
| `experiment_id` | 正規化された実験定義の決定的ID |
| `analysis_mode` | `signal_validation`または`executable_validation` |
| `temporal_oos` | Testが時系列上の選択情報から隔離されたこと |
| `point_in_time_universe` | 全foldのUniverse Snapshotが各Test開始以前か |
| `survivorship_bias_status` | `not_indicated_by_snapshot_timing`または`present` |
| `formal_oos_eligible` | 機械検証可能なFormal OOS前提の合否 |
| `formal_oos_ineligibility_reasons` | 不適格理由codeの配列 |
| `formal_oos_claim_scope` | 人の過去Test閲覧までは証明しないという主張範囲 |

`experiment_id`は次の形式です。

```text
wf3-<analysis_mode>-<64 lowercase hexadecimal SHA-256>
```

SHA-256の入力は、Phase 3設定、base Strategy設定、mode固有Candidate Catalog、Selection Policy、Fold Schedule、Provider、analysis mode、Canonical Schema version、`provider_price_basis`、Canonical semantic content hash、normalized Universe hash、`universe_as_of_date`、random seed、Git commit、source-tree hash、source stateからなる正規化JSONです。

Experiment IDとArtifact hashは別の契約です。Experiment IDは分析に影響する実験定義を識別し、Artifact hashは完成した各出力fileのbytesを検証します。runtime時刻、lineage、入力の`fetched_at`、入力行順だけの差はExperiment IDに混入しません。

## `source`

| Field | 意味 |
|---|---|
| `source_state` | `clean`、`dirty`、`git_unavailable` |
| `git_commit_sha` | 利用可能な場合のGit commit |
| `git_branch` | 利用可能な場合のbranch |
| `worktree_dirty` | Gitが利用可能な場合のdirty状態 |
| `source_tree_sha256` | 追跡・非ignore対象source treeの内容hash |
| `reproducibility_status` | `reproducible`または`degraded` |

`dirty`と`git_unavailable`は再現性を`degraded`とし、Formal OOS不適格です。`git_unavailable`ではGit metadataをnullで表します。完成Manifestへlocal absolute pathや`git_root`を出力しません。

## `runtime`

`started_at_utc`、`completed_at_utc`、`python_version`、`platform`、`stock_range_trader_version`、`pandas_version`、`numpy_version`、`pyarrow_version`、`pyyaml_version`、`provider_library_version`を記録します。runtime時刻とversionは再現性の観測情報であり、Candidate selection情報ではなく、Experiment IDにも含めません。

## `inputs`

| Field | 意味 |
|---|---|
| `input_filename` | 入力Parquetのbasename |
| `requested_start` | 要求半開区間の開始日 |
| `requested_end_exclusive` | 要求半開区間の排他的終了日 |
| `actual_start`／`actual_end` | 実観測の最初／最後の日 |
| `canonical_schema_version` | Canonical Schema version |
| `input_file_sha256` | 入力file bytesのSHA-256 |
| `canonical_content_sha256` | 正規化した分析対象値のSHA-256 |
| `row_count` | 入力行数 |
| `column_names` | 入力列名と順序 |

Canonical semantic hashは`fetched_at`を除き、`symbol`と`date`で安定sortした分析値から計算します。このため、`fetched_at`や入力行順だけの非意味的な差をExperiment IDへ混入させません。一方、`input_file_sha256`は元file bytesの識別に残します。

API Key、token、環境変数値、local absolute path、Raw市場データはManifestへ保存しません。

## `universe`

| Field | 意味 |
|---|---|
| `universe_filename` | Universe CSVのbasename |
| `file_sha256` | Universe file bytesのSHA-256 |
| `normalized_universe_sha256` | 正規化Universe全体のSHA-256 |
| `universe_as_of_date` | Snapshot基準日 |
| `included_symbol_count` | Universe内銘柄数 |
| `available_price_symbol_count` | Universe内かつ価格ありの銘柄数 |
| `missing_price_symbol_count` | Universe内だが価格なしの銘柄数 |
| `unexpected_price_symbol_count` | Universe外だが入力に存在した銘柄数 |
| `point_in_time_universe` | 全foldのSnapshot timing判定 |
| `survivorship_bias_status` | Snapshot timingに基づくbias状態 |
| `snapshot_timing_claim_limit` | timing判定だけでは完全復元にならない制約 |
| `fold_assessments` | fold別の`temporal_oos`、`point_in_time_universe`、bias状態 |
| `coverage_file` | `universe_coverage.csv` |

`universe_as_of_date > test_start`のfoldが1つでもあればrun全体の`point_in_time_universe=false`、`survivorship_bias_status=present`です。`not_indicated_by_snapshot_timing`は完全なSurvivorship bias排除を意味しません。上場廃止、銘柄コード変更、売買停止等は完全復元していません。

## `provider_capability`と`price_policy`

`provider_capability`は`provider`、`signal_validation_supported`、`executable_validation_supported`、`benchmark_supported`、`provider_price_basis`、`maximum_expected_history`、`availability_lag`、`notes`に、`observed_input_actual_start`と`observed_input_actual_end`を加えた実行時契約です。未知Providerやmode非対応は計算前にfail-closedとなります。

`price_policy`は次のfieldを持ちます。

- `provider_price_basis`
- `signal_price_mode`
- `execution_price_mode`
- `dividend_policy`
- `corporate_action_mode`
- `theoretical_benchmark_mode`
- `executable_benchmark_mode`

Signal modeで適用不能なExecutable項目に架空の数値を作りません。`yfinance_unsupported`や`not_applicable`相当の契約値で非適用を表現します。Providerを跨いだ時系列連結、暗黙fallback、欠損期間の別Provider補完は禁止です。

yfinanceはProvider調整済みSignal lane専用で、Executable結果と両Benchmarkは常に`unsupported`です。J-Quants ExecutableはProvider報告Execution laneを使いますが、価格basisと企業行動契約を満たさない銘柄は`unsupported`として除外します。配当はStrategyとBenchmarkの利益へ別加算しません。

## `configuration`

| Field | 意味 |
|---|---|
| `phase3` | 正規化済み`Phase3Config`全体 |
| `base_strategy` | 固定`StrategyConfig` |
| `candidates` | analysis mode固有Candidate Catalog |
| `random_seed` | 設定された決定的seed |
| `phase3_config_file` | Phase 3設定のbasenameとfile SHA-256 |
| `strategy_config_file` | Strategy設定のbasenameとfile SHA-256 |

秘密値は含めません。CandidateはCatalog順を保持し、最大12件です。Signal Candidateは`buy_atr_multiplier`、`range_score_threshold`、`adx_entry_max`だけ、Executable Candidateはそれらに`sell_atr_multiplier`を加えた項目だけを変更できます。

## `fold_schedule`、`selection_policy`、`test_policy`

`fold_schedule`は`config`、`configured_start`、`configured_end`、`folds`を持ちます。`config`には`train_months`、`validation_months`、`test_months`、`step_months`、`forward_sessions`、`embargo_sessions`、`minimum_folds`、`purge_rule`が入ります。各foldには`fold_id`、`train_start`、`train_end`、`validation_start`、`validation_end`、`test_start`、`test_end`、`embargo_sessions`が入ります。日付区間はすべて`[start, end)`で、実観測境界はCSVへ別記します。

Signal Forward Labelは銘柄別の実観測sessionで決め、`embargo_sessions >= forward_sessions`、`label_end_date < test_start`を必須とします。`label_end_date == test_start`はPurgeし、Test末尾でhorizon不足の観測は`right_censored_at_test_end`として除外します。

`selection_policy`はmode固有です。Signalは`mean_reversion_target_hit_rate`をPrimaryとし、`median_forward_return_desc`、`median_mae_magnitude_asc`、`candidate_id_asc`で同順位を決めます。Executableは最低取引条件と最大DD限度を適用後、`median_symbol_sharpe_ratio`、`median_symbol_maximum_drawdown_magnitude_asc`、`median_symbol_net_return_desc`、`candidate_id_asc`を使用します。

`test_policy`は現在、次の固定値です。

| Field | Value |
|---|---|
| `candidate_selection_information_set` | `validation_only` |
| `test_candidate_count_per_fold` | `0_or_1` |
| `fallback_after_test_failure` | `forbidden` |
| `test_result_used_for_reselection` | `false` |
| `test_state_reset` | `true` |
| `test_end_rule` | `exclusive` |
| `test_reexecution_for_reporting` | `false` |

TestはValidationで選ばれた1候補だけを一回評価します。`no_eligible_candidate`では0候補となりTestを実行しません。Test失敗後のfallback、Test結果による再選択、Report作成時のTest再実行は禁止です。

## `results`

共通fieldは`fold_count`、`evaluated_fold_count`、`no_eligible_fold_count`、`aggregate_status`、`candidate_selection_counts`です。`candidate_selection_counts`の各entryは`candidate_id`、`selected_fold_count`、`selected_fraction_of_all_folds`、`selected_fraction_of_evaluated_folds`を持ちます。

### Signal mode

`SignalWalkForwardAggregate`に一致する次のfieldを追加します。

- `test_observation_count`
- `unique_test_symbol_count`
- `mean_reversion_target_hit_rate`
- `median_forward_return`
- `median_mae_magnitude`

これは選択CandidateのTest Signal観測をpoolした分布です。`forward_return`、Target Hit、MAEはProvider調整済みSignal価格上のOutcomeであり、FillやPortfolio利益ではありません。

### Executable mode

`ExecutableWalkForwardAggregate`に一致する次のfieldを追加します。

- `requested_symbol_fold_count`
- `admitted_symbol_fold_count`
- `traded_symbol_fold_count`
- `total_trade_count`
- `finite_sharpe_symbol_fold_count`
- `median_symbol_fold_sharpe_ratio`
- `median_symbol_fold_maximum_drawdown_magnitude`
- `worst_symbol_fold_maximum_drawdown_magnitude`
- `median_symbol_fold_net_return`

各値は独立資金`symbol-fold`結果の分布です。zero-tradeのadmitted `symbol-fold`はReturn／DD分布へ含み、非有限SharpeだけをSharpe集計から除外します。共通Portfolio、銘柄間の資金競合、fold連結複利を表しません。

## `exclusions`

`counts`は`stage`、`scope`、`status`、`reason`ごとの件数、`excluded_symbol_count`は`scope=symbol`の一意銘柄数、`details_file`は`walk_forward_exclusions.csv`です。

`unsupported`はProvider価格basisや企業行動を安全に処理できない状態、`excluded`はデータ不足、Purge、right-censoring等を表し、意味を混同しません。個別銘柄の`unsupported`／`excluded`は可能な場合に他銘柄の評価を継続します。一方、未知Provider、mode非対応、Provider混在等のCapability違反は実験全体を計算前にfail-closedとします。

## `artifacts`とbundle構成

Manifest以外の全成果物について、各entryは次を持ちます。

| Field | 意味 |
|---|---|
| `filename` | bundle直下のCSV名 |
| `sha256` | 書き込み済みfile bytesのSHA-256 |
| `size_bytes` | byte size |
| `row_count` | headerを除くCSV行数 |
| `columns` | 空CSVでも維持する固定列Schema |

実ファイルから再計算したhashが`sha256`と一致しなければbundleは破損です。`walk_forward_manifest.json`自身は`artifacts`へ含めません。

共通9 CSV:

1. `walk_forward_folds.csv`
2. `fold_observation_bounds.csv`
3. `validation_cohort.csv`
4. `universe_coverage.csv`
5. `candidate_validation_results.csv`
6. `selected_parameters.csv`
7. `candidate_selection_frequency.csv`
8. `walk_forward_exclusions.csv`
9. `walk_forward_summary.csv`

Signal専用2 CSV:

1. `oos_signal_observations.csv`
2. `oos_signal_fold_summary.csv`

Signal bundleは共通9＋Signal 2＋Manifest 1の**12ファイル**です。

Executable専用4 CSV:

1. `oos_executable_metrics.csv`
2. `oos_trade_log.csv`
3. `oos_order_log.csv`
4. `oos_equity_curve.csv`

Executable bundleは共通9＋Executable 4＋Manifest 1の**14ファイル**です。

## `lineage`

`parent_experiment_id`と`change_reason`は両方null、または両方指定します。lineageはExperiment IDへ含めません。lineage付き実験は、過去Test確認後の変更を隠さない派生実験としてFormal OOS対象外です。

## `limitations`と`warnings`

`limitations`は現在、少なくとも次を固定表示します。

- 上場廃止、銘柄コード変更、売買停止の完全復元なし
- future Universe foldにはSurvivorship biasがある
- yfinanceはSignal専用
- yfinance調整値に分配影響が含まれ得る
- J-Quants Freeの履歴・遅延制約
- unsupported corporate action銘柄の除外
- Executableは独立資金`symbol-fold`分布
- 共通Portfolioなし
- 税金未対応
- Test成績は将来利益を保証しない

`warnings`はFormal OOSの主張範囲とUniverse Snapshot timingの限界を常に含みます。runが不適格なら`formal_oos_eligibility_not_satisfied`、future Universe foldがあれば`one_or_more_folds_use_a_future_universe_snapshot`を追加します。STEP 11のLive運用状態や外部library warningのために、Manifestへad-hoc fieldを追加しません。

## 原子的公開と上書き禁止

1. 一時directoryへ全CSVを書き込む
2. 各CSVのSHA-256、byte size、row count、columnsを計算する
3. Manifestを最後に書く
4. file集合、Schema、provenance、ID、hashを検証する
5. 完成bundleを`<output-dir>/<experiment_id>/`へrenameする

途中失敗では一時directoryを除去し、部分bundleを完成出力として残しません。同じExperiment IDを上書きせず、自動suffix、暗黙削除、`force`上書きも行いません。失敗receiptは`_failed/`へ別の安全化JSONとして原子的に保存され、例外messageや秘密値を記録しません。

## Consumer検証手順

Consumerは少なくとも次を確認してください。

1. `schema_version == "phase3-report-1.0"`か
2. `status == "completed"`か
3. modeとExperiment ID prefixが一致するか
4. mode固有のCSV集合が12／14ファイル契約と一致するか
5. `artifacts`のfilename、columns、row count、size、SHA-256が実fileと一致するか
6. Provider、`provider_price_basis`、Fold ID、Candidate IDがManifestと全CSVで一致するか
7. `formal_oos_eligible`、Universe timing、lineage、limitations、warningsを結果解釈に反映したか

## Versioning規則

- 誤字修正や説明強化だけではSchema versionを変更しません。
- field削除、rename、型変更、意味変更はbreaking changeです。
- breaking changeでは`REPORT_SCHEMA_VERSION`を更新し、Builder、Validator、テスト、README、本仕様書を同じ変更で更新します。
- additive fieldもConsumerへの影響を評価し、minor相当のversion更新要否を明示判断します。
- CSV schema変更とManifest schema変更は別の契約として追跡します。
- STEP 12は現在の実装を文書化するだけなので、`phase3-report-1.0`を維持します。
