# PR #7統合後のローカルブラウザ検証

## 対象と境界

- 起点: PR #7のマージコミット `833eacad6d5a0de5ae8a34dcc07067fea35911e5`。
- マージ先: `codex/phase3-walk-forward-validation`。専用ブランチ: `codex/observability-browser-tests`。
- 専用の別worktreeから実行。承認済み6月worktree、plan・設定・実装hash・市場データ・台帳は変更しない。
- 変更はテストと文書のみ。画面本体、戦略、Proxy清算、価格契約、Gate、研究結果の計算規則は変更しない。
- 市場API通信、実データ再清算、正式登録、OOS開始、mergeは行わない。

## 実行環境と切り分け（2026-09-22 JST）

macOS 15.7.7 arm64、Python 3.13.5、Google Chrome 153.0.8010.48
（CDP protocol 1.3、revision `199a3a541d76237379e353b348e64045584db057`）。
依存追加・ブラウザのダウンロードは行っていない。

空のローカルHTMLと人工口座HTMLを、元の起動オプション・同じ40秒制限で比較した。

| 起動経路 | 空ページ | 人工口座HTML |
| --- | --- | --- |
| 元の`--dump-dom` | 40.026秒でtimeout。DOM出力済み・親と子3個が生存 | 40.038秒でtimeout。操作マーカー出力済み・親と子3個が生存 |
| 上記に`--use-mock-keychain`だけ追加 | 0.799秒、exit 0、残存なし | 40.057秒でtimeout。DOM出力済み・親と子3個が生存 |
| CDP pipe・テスト用Keychain・Crashpad無効・明示的`Browser.close` | 正常終了、exit 0、残存なし | 正常終了、exit 0、残存なし |

元のtimeout時の親プロセス`poll()`はどちらもNoneだった。単なるstdoutのEOF待ちではなく、
Chrome本体の自動終了待ちで停止していた。元の空ページは診断後のSIGTERMでも終了せず、
当該一時プロファイルのプロセス群だけを確認してSIGKILLで回収した（exit -9）。
元の口座HTMLは診断後のSIGTERM後にexit 0になったが、**timeout／強制回収なので失敗**として扱った。

stderrにはKeychain lookup失敗、表示リンク／GPU関連エラーがあった。
Keychain指定だけでは口座HTMLの停止を解消していないため、Keychainを唯一の原因とは断定しない。
確認できた原因範囲は、旧テストがChromeの`--dump-dom`自動終了経路に依存し、
読込・操作・終了を区別できず、timeout時も子プロセス群まで管理していなかったこと。
Chrome内部の停止スタックそのものは未特定。画面JSの成功マーカーをpytest成功へ読み替えていない。

## 補正後の契約

`tests/browser_session.py`はテスト専用であり、本番の読取・出力Interfaceへ組み込まない。

1. 毎回新規の一時profile、別process group、DevTools pipe（fd 3/4）で起動する。
2. `Browser.getVersion`で応答を確認し、空ページ→未改変の口座HTMLを順に読み込む。
3. `location.href`と`document.readyState === 'complete'`を確認してから操作する。
4. CDP Inputによるマウス／キー操作を行い、`isTrusted`のinput/change/clickを確認する。
   日付は人工fixtureにない1月1日を入力・解除。銘柄／statusはネイティブ先頭文字検索で選択。
   注文詳細はクリックで開閉し、表示内容を同じ読取headの①独立監査と照合する。
5. 列見出しのクリックで昇順・降順を検証する。Decimal相当の比較、巨大数、小数、負値、0、
   欠測の末尾配置を確認する。データを書き換えたDOM成功マーカーは使用しない。
6. `Browser.close`の応答、親プロセスexit 0、process group消滅を確認して初めて成功にする。

クリック前はスクロール後の2描画frameとhit-testを待ち、実座標へInputを送る。
クリック後も次の描画frameを待つ。select操作後はEscapeでnativeメニューを閉じてから移動する。
この待機なしでは詳細クリックが1回反映されないケースがあったため、成功マーカーや無条件sleepではなく
操作対象の描画状態を確認する方式へ補正した。

全体上限40秒は維持し、起動・空ページ読込・口座読込・操作・終了は各最大10秒かつ残り全体時間以内。
異常時だけ所有process groupへSIGTERM→必要時SIGKILLを送る。強制回収が発生した試験は必ず失敗。
終了済みの親はpoll/waitで回収してからプロセス表を確認し、macOSのkillpg(0)のEPERMを
「残存なし」と誤解しない。ユーザーの既存ブラウザを一括終了する処理はない。

固定オプションは`tests/browser_session.py:FLAGS`と各試験の`lifecycle.json`に記録する。
元のheadless・背景通信無効・同期／拡張／更新無効・外部ホスト解決無効を維持し、
`--remote-debugging-pipe`、`--disable-crashpad-for-testing`、macOS限定の`--use-mock-keychain`を使用する。
HTTP/HTTPS/WS/WSSはページのNetworkドメインでも遮断する。sandboxや証明書検証は無効化しない。
実Keychain、通常のブラウザprofile、待受TCPポートは使用しない。

公式の根拠: [Browser.close](https://chromedevtools.github.io/devtools-protocol/tot/Browser/#method-close)、
[Input](https://chromedevtools.github.io/devtools-protocol/tot/Input/)、
[Chromiumのmock keychain実装](https://chromium.googlesource.com/chromium/src/+/38c29b6535f88af0bbe843e0416390018d965da6/components/os_crypt/sync/os_crypt_mac.mm)。

## 6ケースと合格条件

既存の`rejected / empty / filled / holding / waiting / missing`を維持する。
非空Fill・保有・入力待ち・評価欠測はいずれも人工保存ledgerであり、実市場結果ではない。

- Cash／Equity、simulated_fill数、徴収手数料、保有時価、入力待ち表示をRead Modelと照合する。
- `missing`の未評価表示と徴収手数料0、`rejected / empty`の保有時価0を区別する。
- 口座データのdeep-freeze・操作前後一致、ページresource読込0件を確認する。
- 原本全ファイルhashと監査headを操作前後で照合する。取得・清算・書込経路はテスト内で禁止する。
- 別の10件のライフサイクル回帰は人工CDP子プロセスを使う。pipe断片化、通知混在、EOF、protocol error、
  startup/shutdown timeout、非ゼロ終了、JS例外、UI assertion失敗、外部URL拒否、誤ったbrowser指定を検証する。
  これらは実Chromeの6件の代替ではない。

## 再実行と証拠

```bash
ACCOUNT_VIEW_TEST_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  python -m pytest -q -s tests/test_account_view.py -k local_browser

ACCOUNT_VIEW_TEST_BROWSER='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  python -m pytest -q tests/test_order_audit.py tests/test_account_view.py \
  tests/test_preflight_report.py tests/test_trial_comparison.py \
  tests/test_trial_comparison_history.py tests/test_browser_session.py

ruff check .
ruff format --check .
git diff --check
```

各試験のpytest一時ディレクトリ`browser/lifecycle.json`へversion、options、段階の開始時刻、
returncode、failure_phase、forced_cleanup、remaining_process_group、合否を保存する。
stdout/stderrも同じディレクトリに保存する。`-s`では成功した6件の要約も表示する。
一時ログはローカルprofileの絶対パスを含むため、共有時はパスを匿名化し、原本・台帳をGitへ追加しない。

通常CIはbrowser未指定なので6件skipであり、ローカル実Chrome成功と区別する。
他OS・他ブラウザ・将来のChromeバージョンの操作互換性は未検証。
今回のCI結果・ローカル件数・対象SHAはレビュー用PRの説明へ記録する。

## ローカル最終反復結果

同じ最終コードで3回連続実行し、各回 **6 passed / skip 0**（6.89秒、5.90秒、6.01秒）。
3回目のライフサイクル記録は以下。時間はfixture生成を除く起動〜回収まで。

| 人工ケース | 秒 | 終了コード | 強制回収 | 残存process group |
| --- | ---: | ---: | --- | --- |
| rejected | 0.910 | 0 | なし | なし |
| empty | 0.689 | 0 | なし | なし |
| filled | 0.885 | 0 | なし | なし |
| holding | 0.694 | 0 | なし | なし |
| waiting | 0.861 | 0 | なし | なし |
| missing | 0.687 | 0 | なし | なし |

反復後のOSプロセス一覧にも今回起動したGoogle Chrome／Chrome Helper／Crashpadは残っていなかった。
実行前から存在した別ブラウザのプロセスは対象外として維持した。
