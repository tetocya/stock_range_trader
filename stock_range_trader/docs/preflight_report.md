# ③ 実行前チェックの結果一覧

起点は `bda3570f1da53ea8f9324bbacdab518d03ba4d1e`。①②の読取・原子的出力を
再利用する診断専用ツール。API認証・公式サイト確認・取得・清算・復旧・許可生成・
登録・正式OOSは行わない。④比較機能なし。結果は実行Gateや所有者の承認を代替しない。

## 実行・終了コード

```bash
python -m research_tools.preflight_report \
  --trial-root JUNE_ROOT --may-root MAY_ROOT \
  --stage acquisition --output outputs/preflight-UNUSED_NAME
```

`--stage` は acquisition / build_inputs / clearing / resume_account / all（既定）。
再開対象は `--account research_split.sqlite` のようにtrial内相対パスで明示する。
許可の既定名は `owner_approved_acquisition.json` / `owner_approved_clearing.json`。
別名は `--acquisition-authorization` / `--clearing-authorization` で指定する。
templateから承認を補完しない。口座を推測選択・作成しない。

| code | 意味 |
|---|---|
| 0 | 出力成功、指定した各段階の必須項目すべてpass。実行はしていない |
| 2 | 出力成功、blocked／unverified等で指定段階がreadyではない |
| 3 | 内部不整合を記録したレポート、または読取・出力契約違反で生成できなかった |

標準出力 `report_written` で生成成否を区別する。既存出力は上書きしない。
CLIは実時刻のみで、時刻上書きオプションなし。Unit Testは時計を注入する。
すべての結果に `diagnostic_only=true` を記録し、自動起動の入口を設けない。

今回の具体例：

```bash
cd /Users/harimatakeuchi/stock_range_trader_observability/stock_range_trader
/Users/harimatakeuchi/stock_range_trader/stock_range_trader/.venv/bin/python \
  -m research_tools.preflight_report \
  --trial-root /Users/harimatakeuchi/stock_range_trader/stock_range_trader/.delayed_replay/june_trial/46890-202606-clearing-preparation-v3 \
  --may-root /Users/harimatakeuchi/stock_range_trader/stock_range_trader/.delayed_replay/selected_trial/owner-approved-fixed-baseline-v1 \
  --stage all --output outputs/preflight-june-v1
```

## 段階別の判定

| 段階 | 主な必須条件 | 不要な条件 |
|---|---|---|
| acquisition | plan／実装／設定、保存履歴・単元、取得許可、日時、現在の公式提供確認、台帳・残予算、キー存在、実データ由来 | 清算plan・清算許可・口座 |
| build_inputs | 共通証拠、4要求の応答完了、calendar・master・履歴比較・価格、prefix指標有限性 | 残HTTP予算、生成前のinput manifest・清算plan |
| clearing | 入力構築条件、保存input manifest／packet、清算plan、別個の清算許可 | 現在のHTTP予算・キー |
| resume_account | 清算条件、明示口座のhash連鎖・head・identity・cursor・受理packet・再送整合性 | HTTP取得、Reducer再生 |

新規清算で口座未指定なら口座の存在は非必須not_applicable。
再開で未指定ならunverified。既存口座を指定すればidentityも必須。
口座headは許可と独立に検査する。保存入力とpacket・価格原本も照合するが、
全状態遷移を再生して証明するものではない。最終判断は既存実行Gateに残す。

各チェックは pass / blocked / unverified / not_applicable / error、固定理由コード、
確認時刻、必須フラグ、証拠hash、次の作業を持つ。複数理由を保持する。
集約の優先順位は error > blocked > unverified > not_applicable。
**必須項目がすべてpassの場合だけ** `ready_for_<stage>=true`。
「全体が実行可能」というフラグは出さない。認証履歴は非必須の観測項目。

## 日時・公式提供範囲・キー

既存Gateと同じ2026-09-24 18:00 JST、12週間遅延・730日遡及の日付条件を使用する。
境界一致は日時条件を通過するが、公式提供確認にはならない。
`authorized_local_preflight.json` を読み、Schema・plan・日時を検査する。
現行planには保存された提供確認の再利用Schema・鮮度契約がないため、過去の記録や
単なるverified表記から現在の提供範囲をpassへ昇格させない。初版ではunverifiedを維持する。
独自の24時間期限等は追加しない。したがって日時通過後も取得readyは安易にtrueにならない。

キーは現在プロセスの `JQUANTS_API_KEY` の存在だけをBooleanで確認する。
値・キーhash・環境変数一覧は出力しない。存在だけで認証成功にしない。
台帳にHTTP 200があれば過去の応答時刻を示すが、現在の認証は通信しないためunverified。
人工応答はその出自を明記する。

## 読取専用性・再利用

既存 `june.inspect()` は内部で書込可能なReceiptを開くため直接呼ばない。
その純粋な `Receipt.events/statistics/selection` をreadonlyアダプターで再利用し、
人工データで既存inspectと統計を照合する。Receiptのconstructor／append／remaining、
lease・予算初期化を呼ばない。①②の `read_database` へmode=ro／query_only／
SQL authorizer／transactionを共通化。WAL・journalは接続前に拒否し、repair等はしない。

未開始は0試行、started_at／deadlineはnullのまま。
開始後は最初のattemptから既存契約の1200秒deadlineを算出表示するだけで、保存しない。
20試行上限、最終試行時刻、停止履歴、時計逆行、13秒間隔を確認する。
期限切れと試行上限を別項目にし、複数理由を保持する。再検査でリセットしない。

JunePlan、SelectedTrialPlan、require_lot、InputPacket、receipt_captures、compare_history、
sessions、split_sessions、daily等の純粋な検証を再利用する。
選択再計算を避けるためSelectedInputs.loadは呼ばず、固定parent／packet／価格原本／
履歴hashの結合を①の価格証拠照合で検査する。指標は固定設定のprefix検証のみでSignalなし。
build(verify_only=True)も内部で書込Receiptを開くため直接呼ばず、保存契約を照合する。
既存コード更新時にはこれらの互換性テストを再実行する必要がある。

## 成果物と検証

- preflight_checks.json：preflight-checks-v1
- preflight_checks.csv：文字列の数式対策付き
- preflight_report.html：escape済み、JavaScript・外部CDNなし
- report_manifest.json：preflight-report-manifest-v1。ツールSHA/hash、入力ID/head、確認時刻、段階判定、成果物hash

①のpublish_artifactsで原子的公開。May／Juneの両入力から出力を分離する。
公開前に原本の変更・証拠ファイル追加を再検査し、途中変更なら中断する。
許可本文・Raw Data・不要な絶対パスを出力しない。hash一致は真正性の証明ではない。

```bash
python -m pytest -q tests/test_preflight_report.py tests/test_order_audit.py tests/test_account_view.py
python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

人工テストで日時境界、予算各状態、欠落と破損、許可分離、WAL、反復不変、
口座再開条件、書込・通信・選択・清算禁止を確認する。Live skipは未検証。
承認済み6月worktree・設定・実装hash・許可・台帳は変更しない。
③のローカルcommit・報告で停止し、④・push・PR・mergeは行わない。
