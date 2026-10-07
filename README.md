# JEPA-ScenePick (Mac ローカル試作)

短い見本区間から、ローカル動画群の似た時刻を探すためのJEPA試用成果です。今回は実映像を使わず、OpenCVで作った色付き図形の合成動画だけで実装と性能を確認します。検索結果には元動画のパスと秒単位の開始・終了時刻を返し、`keep` / `hold` をengine別に記録して参照リストを作れます。画面は「動画を追加して索引を準備」→「見本区間を選んで検索」→候補比較の順に進み、索引準備後は設定を畳んで区間検索を前面に表示します。

## プライバシー境界

- このリポジトリに含める動画は自作図形の合成サンプルだけです。実動画、索引DB、埋め込み、モデルcheckpoint、依存環境、利用者データは同梱しません。
- `Movies`、放送映像、ユーザー動画、音声、フレーム、サムネイルには一切アクセスしていません。提供する小さな動画サンプルは自作図形の合成素材だけです。再現テストが生成する素材とDBは一時ディレクトリに作成します。合成ジェネレータはMoviesディレクトリ配下と既存ファイルへの出力を拒否し、上書きしません。
- 検索・索引処理にHTTP/API/テレメトリ送信機能はありません。V-JEPA processorは同梱JSONから作成し、raw checkpointを指定済みローカルパスから元loaderで開きます。HF Hubモデルsnapshotへのアクセスやfallbackはありません。欠落時はエラー停止します。依存インストールも実行時処理から分離しています。
- これはコード経路の確認であり、OS全体のネットワーク遮断や「絶対に送信されない」保証ではありません。合成CLIテストでは子Pythonプロセスの `socket.connect` / `create_connection` / `urllib.request.urlopen` を例外化し、その条件下で生成・索引・検索・採否・参照リストまで成功しました。これはPythonレベルのこれらAPIに限った観測です。OpenCV/FFmpegのネイティブコード、他プロセスやOS全体の通信は捕捉せず、外部通信の絶対不在や独立した通信監査を証明しません。
- 索引SQLiteには、利用者が後日指定する動画のローカルパス、サイズ、更新時刻、特徴量、元動画時刻が保存されます。ファイル名やパスは外部へ送る設計ではありません。現在のbaseline特徴量は4x4色グリッドの平均と単純な動き量です。

## 準備・実行

動作対象は Python 3.12/3.13、macOS arm64。依存の版は `pyproject.toml` と `requirements-model.txt` に固定しています。現在の検証端末はPython 3.14なので、依存を導入せず、既存のOpenCV/NumPy環境でbaseline経路だけを実行しました。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'  # OpenCV/NumPy + test runner; no PyTorch/model weights
# 再現可能なテスト用メディアを生成
PYTHONPATH=src python -m jepasearch.cli synth --output /tmp/jepa-demo.mp4
# 索引と検索 (全入出力はローカル)
PYTHONPATH=src python -m jepasearch.cli index /tmp/jepa-demo.mp4 --store /tmp/jepa-demo.sqlite
PYTHONPATH=src python -m jepasearch.cli search /tmp/jepa-demo.mp4 --start 0 --end 2 --store /tmp/jepa-demo.sqlite
```

検索結果に返る `media_id` と `start_s` を指定して採否を記録します。

```sh
PYTHONPATH=src python -m jepasearch.cli decide --store /tmp/jepa-demo.sqlite --media-id '<media_id>' --start 0 --decision keep
PYTHONPATH=src python -m jepasearch.cli references --store /tmp/jepa-demo.sqlite
PYTHONPATH=src python -m jepasearch.cli bench --output benchmark.json
```

同じ索引への再登録は既存windowを飛ばして再開します。`--no-resume` は登録済み動画を拒否します。同じstoreに対する同時登録はロックで拒否し、容量が推定必要量+256 MiBを下回ればSQLite接続前に停止します（出力親ディレクトリは必要なら作成しますが、DB/sidecarは作りません）。storeのsymlink（danglingを含む）と元動画の同一パス・同一inodeへの出力は拒否します。index後に動画が更新されたことも検出します。近接結果は同動画内1秒以内を抑制します。モデル (`none-baseline-v1`)、前処理 (`rgb-grid-4x4-motion-v1`)、窓 (`2s-stride-0.5s-samples-8-v1`) はSQLiteのwindow行で個別に記録し、複合主キーにも含めます。さらにengine/model/preprocess/window/stride/sample count/feature dtype/device/poolingのfingerprintをpipeline IDへ組み込み、再開・検索・採否はそのIDと現在有効なindexを照合します。設定変更後は別fingerprintへ索引され、特徴を混ぜません。決定レコードもpipeline別に保存します。秒位置はOpenCVが報告するFPSとframe countから計算しており、可変フレームレートや放送タイムコードの精度は未検証です。JEPA特徴は別model/preprocess/window IDで保存され、既存baseline特徴と混ざりません。

## 固定V-JEPA2 raw checkpointの利用

既存 `jepa-risk-detection` の `configs/phase1.yaml`、`src/jepa_risk/features.py`、`src/jepa_risk/predictor.py` と依存宣言だけを参照しました。旧repoのファイルは変更していません。元ローダー `load_official_bundle()` を通して既存の Meta 形式 `vitl.pt` をstrictに読み込み、目標encoder/predictorは破棄してViT-L encoderを利用します。ロードは `torch.load(..., weights_only=True)` です。固定識別子はV-JEPA2 ViT-L/16、FPC64、公式HF revision `b3c1679b7c34d3255ef3547f27c7b226aefab26f`、元Meta loader commit `204698b45b3712590f06245fbfba32d3be539812` です。processor設定はそのrevisionの公式設定を小さなJSONとして同梱し、processorをメモリ内で直接構築します。HF Hubからの取得/モデルsnapshot fallbackはありません。

ローカル設定 `artifacts/checkpoints/vitl.pt` は5,127,726,842 byte（約4.78 GiB）で、既存のraw checkpointを再利用しました。新たな重み取得量は0 byteです。HFの固定revision一覧はリポジトリ合計6.43 GB、`model.safetensors` 単体1.3 GBを表示しますが、この実装はHF safetensors snapshotを使わずraw `.pt` を元ローダーで読みます。公式processor設定は約1.3 KBです。[固定revisionの公式ファイル一覧](https://huggingface.co/facebook/vjepa2-vitl-fpc64-256/tree/b3c1679b7c34d3255ef3547f27c7b226aefab26f) と [Meta FAIR公式repo](https://github.com/facebookresearch/vjepa2) を参照してください。

新規利用者がこの既存raw checkpointを使わない場合は、モデル本体を別途安全に用意する必要があります。checkpoint pathは `JEPA_RISK_ROOT` / `JEPA_RAW_CHECKPOINT` で指定できます。baseline依存とモデル依存は分離し、`requirements-model.txt` と `pyproject.toml` のmodel extraに固定版を書いています。実行時のprocessorは同梱JSONのみで、checkpoint欠落やloader/shape不整合はエラー停止します。実行時自動取得やAPIはありません。今回の統合計測は旧プロジェクトの既存venv（Python 3.12.13、torch 2.14.0、transformers 4.57.6）を読取専用で利用しました。新規環境用に宣言したtorch 2.7.1/transformers 4.53.3の組合せはまだ別途検証していません。

検索queryの区間長は索引窓と一致する2秒のみ受け付けます。許容誤差は±0.000001秒（1µs）で、これを外れる区間や有限値でない時刻は動画を開く前にエラーで拒否します。V-JEPAの入力は64 RGB frames / 2 seconds、公式processorのshortest-edge 292 resizeと256x256 center crop、32x16x16のViT token mean poolingで1024次元です。色/動きbaselineとはmodel, preprocessing, window versionを別々に記録し、検索対象もpipeline別です。索引メタデータの動画パスはローカルSQLiteに保存されます。URL・RTSP/TCP/file URI・UNC・playlist/link拡張子・playlist内容を動画として渡すのは拒否します。拡張子だけでなくMP4/MOV/AVI/Matroska/MPEG/TSの許可署名を照合してから、OpenCVのFFmpeg backendへ渡します。decoder open時はFFmpeg `protocol_whitelist=file` を指定します。これは入力境界の防御で、OS全体の遮断や任意のnative backend/OS経路に対する絶対保証ではありません。明示パスがネットワークmountを指すかの完全判定までは行いません。

## 合成ベンチマークと限界

`benchmark.json` は軽量baseline、`benchmark-vjepa.json` はraw checkpointによる1窓の統合計測です。Mac arm64/MPSで合成MP4 36,226 byte、1窓、SQLite 32,768 byte、decode 0.029秒、前処理+ViT推論2.003秒、保存0.0004秒、モデル読込込みindex 8.463秒、モデル再読込込みquery/search 6.347秒、1024次元、ピークRSS 8,512,323,584 byte、デバイスMPSでした。検索は元の0–2秒区間を1候補、cosine 1.0で返し、keep参照にも記録しました。モデルロード前後のプロセス計測を含むため、RSS値は1窓推論専用の増分メモリではありません。1窓の値をフル試合や大量素材へ外挿できません。

別測定の軽量baselineはPython 3.14.4 / OpenCV 4.13.0 / NumPy 2.4.4、6秒合成動画40,981 byte・9窓、SQLite 49,152 byte、decode 30.2 ms、feature 6.4 ms、保存1.6 ms、index 41.5 ms、search 4.9 ms、peak RSS 115,720,192 byteでした。このbaseline計測環境ではPyTorch未導入です。raw V-JEPAの成功したforwardは既存venvで `mps_available=true` を確認してMPSに載せています。過去の別runtime情報照会では `mps_available=false` を返す不整合がありましたが、今回のモデルforwardで実際に `device=mps` と出力 shape・有限値を確認しています。別Macや別venvでのMPS互換性・CPU所要時間は未確認です。

合成素材の結果は接続確認と負荷1回の記録であり、バスケ放送映像やヘルメットPOVスキー映像での検索品質を証明しません。近接重複抑制、VFR/放送タイムコード、長時間の停止再開や容量見積もりの精度、フル試合の速度/メモリ、CPU fallback、mounted filesystem判定、別依存版での再現性は未検証です。システム全体のネットワーク遮断を設定していません。基礎CLIのネットワークガード試験と合成offline実行は通りましたが、Pythonの監視対象外となるネイティブコード・他プロセス・OSレベル通信の絶対的不在までは主張しません。

## 再現方法

このMacでは次でraw checkpointを使った合成の索引・検索・採用・参照リストを再実行できます。既存venvを変更せず、HF offline環境変数を設定し、一時ディレクトリに合成動画とDBを作ります。

```sh
./scripts/run_existing_venv_cli.sh
```

新しいbaseline環境はREADME冒頭のeditable install手順から作れます。V-JEPA本体を使う `index` / `search` は `--engine vjepa2` と必要なら `--checkpoint /local/path/vitl.pt` を指定します。既定checkpointは隣接する `jepa-risk-detection/artifacts/checkpoints/vitl.pt` です。モデル欠落時は自動取得せず停止します。

```sh
PYTHONPATH=src python -m jepasearch.cli index /local/clip.mp4 --store /local/index.sqlite --engine vjepa2
PYTHONPATH=src python -m jepasearch.cli search /local/query.mp4 --start 10 --end 12 --store /local/index.sqlite --engine vjepa2
```

## MacローカルUI

### 検索前の索引確認

検索は、選択中の動画・検索エンジン・現在の保存先の組合せに完了済み索引がある場合だけ開始します。画面の検索エンジンを変えた場合や保存先を切り替えた場合は、その組合せに索引があるかを確認してください。ページ再読み込み後は最後に使ったジョブのエンジンを復元します。索引がない場合は画面内に案内を表示し、APIも検索ジョブを作らず409で返します。索引ジョブが失敗・中断した場合は完了扱いにしません。保存先ごとの索引情報は切り替え後も保持し、既存の索引DBは削除しません。

### 合成のお試し動画

合成動画は [`samples/synthetic-court-motion.mp4`](samples/synthetic-court-motion.mp4) にあります。UIでこのファイルを選んで「Color + Motion」を索引し、見本動画に同じファイルを選択してください。開始時刻を `1` 秒にすると `00:01–00:03` の2秒区間を検索できます。自作図形の動きとカットだけを含む6秒の動画です。再生成する場合は次を実行します。既存ファイルは上書きせずエラーになります。

```sh
PYTHONPATH=.runtime-deps:src python3 -c 'from pathlib import Path; from jepasearch.synthetic import make_video; print(make_video(Path("samples/synthetic-court-motion.mp4"), duration=6, fps=15))'
```

この索引テスト用動画はMPEG-4 Part 2 (`mp4v`) です。Chrome 154ではこのcodecを再生できないことを確認しました。Chromeでプレーヤーも試す場合は、同じ合成素材から作ったH.264 Baseline版 [`samples/synthetic-court-motion-h264.mp4`](samples/synthetic-court-motion-h264.mp4) を使います。この動画を事前表示する固定デモは、実素材やファイル選択を使わず次で起動できます。

```sh
PYTHONPATH=.runtime-deps:src python3 scripts/serve_synthetic_ui_demo.py --port 8770
```

H.264版を作り直す場合（ffmpeg/libx264が必要、出力先が既にあれば上書きせず停止）：

```sh
ffmpeg -n -i samples/synthetic-court-motion.mp4 -map 0:v:0 -an -c:v libx264 -preset medium -profile:v baseline -level 3.0 -pix_fmt yuv420p -movflags +faststart samples/synthetic-court-motion-h264.mp4
```

Python標準ライブラリのHTTP serverと同梱CSS/JavaScriptを使う小さなUIです。外部UI framework、CDN、Web font、analyticsはありません。Safari/Chromeなどのブラウザから `127.0.0.1` のみで開きます。Tkinterは必須ではありません。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m jepasearch.ui --port 8765
```

起動時に `http://127.0.0.1:8765/` を表示します。自動でブラウザが開かなければ同じURLを手動で開きます。`--no-open` は自動起動を抑止し、`--port 0` は空きportを選びます。V-JEPA model extra/ローカルcheckpointの条件は上記モデル節のとおりです。重みがない場合は取得せず、画面のエラーに出します。`Color + Motion` はPyTorchなしで動きます。

V-JEPA 2を使うUIは既存モデルvenvのPython 3.12で起動してください。`open_ui_server.command` はオフライン環境変数と既存モデルvenvを設定し、8767番でユーザー起動のバックグラウンドprocessを開始します。ログは保存せず、OSの常駐設定も追加しません。8766番など指定portが使用中なら何も停止せずエラーになります。

```sh
JEPA_UI_PORT=8767 sh scripts/manage_ui_server.sh start
JEPA_UI_PORT=8767 sh scripts/manage_ui_server.sh status
# 終了時
JEPA_UI_PORT=8767 sh scripts/manage_ui_server.sh stop
```

Terminalを直接開く場合は `open -a Terminal scripts/open_ui_server.command` を実行するか、この `.command` をFinderから起動できます。8766番の以前のprocess/sessionはこの起動操作では停止・変更しません。

URLは `http://127.0.0.1:8767/` です。終了・再起動すると動画の選択とsessionは失われます。索引データは既存の保存先に残り、同じ動画と保存先を選んで索引を再開できます。

「動画を選択」と「保存先を選ぶ」はmacOSのネイティブdialogです。ファイルは1本ずつ選び、追加で繰り返せます。ブラウザからの動画uploadや任意pathを読むAPIはなく、選択pathは同じローカルprocess内に保管し、HTTPにはsession所有のopaque file IDだけを使います。選んだ素材と保存先はlocalhost上の同一sessionに束縛されます。サーバーが動作している間は画面を閉じて再接続しても、同じsession cookieがあればjob状況へ戻れます。serverを終了・再起動した場合は動画を選び直し、同じ索引保存先で「索引を作成 / 再開」を押すと既存fingerprintの窓から続きます。

索引中はモデル起動と窓単位の進捗を表示します。停止は処理中の1窓を終えた後に反映され、次の再開は完了済み窓を飛ばします。同時jobは一つに制限し、別のsessionからは停止できません。検索見本は索引窓と一致する2秒（±1µs）だけ受理します。検索結果では元動画の開始・終了時刻、特徴類似度、結果を生成したエンジンとfeature fingerprintを表示し、類似度は正解確率ではないと明示します。検索エンジンを切り替えた後は、既存候補を生成した方式との違いを表示し、新しい検索まで結果が前の方式のままであることを示します。候補プレビューと見本を並べて比べ、2秒区間と前後各5秒の文脈を切替再生し、採用/保留を記録できます。JSON参照リストはブラウザの標準download先に保存されます。参照JSONには索引元のローカルpathが含まれるので、保存先と共有範囲を利用者が確認してください。

server bindはIPv4 `127.0.0.1` 固定です。Hostをport込みで照合し、状態変更APIにはOrigin、session cookie、CSRF tokenが必要です。APIにpath値を送信できず、再生は選択済みopaque ID、単一Range、選択時と再生直前のファイル署名・inode照合を通ります。decoderは既存coreの許可containerとFFmpeg `file` protocolだけを使います。静的assetは同梱しCSPで外部originを拒否します。出力databaseは既定で `~/Library/Application Support/jepa-video-search/index.sqlite` に置きます。空き容量不足はSQLite作成前に止め、sourceとの同一inode、dangling symlinkを拒否します。

WebKitで表示した画面と操作を合成fixtureだけで確認できます：[JEPA-ScenePick合成UIスクリーンショット](screenshots/ui-redesign-final-20261007.png)。キャプチャ用runnerは検索、タブ矢印移動、前後区間の切替、候補比較、重複した採用操作、エンジン変更後の結果由来表示をDOMイベントで確認します。さらに1024px幅と200% page zoomで横はみ出し、主要パネル、検索ボタンの視認・フォーカス可能性を確認します。これは物理的なマウス/キーボード操作や、画面上での再生品質の手動確認ではありません。test_ui.py はHTTP API/画面構造のテストです。再現コマンドは合成fixtureと一時索引だけを使います。

動画は索引デコーダーで扱えても、ブラウザがそのcodecを再生できるとは限りません。MPEG-4 Part 2の合成動画はWebKitで再生でき、Chrome 154では再生できませんでした。H.264 Baselineの合成版はChrome 154で再生できました。再生error時はプレーヤー上に形式/codecの案内を表示します。実映像評価前に、利用予定のブラウザで試してください。

```sh
scripts/capture_synthetic_ui_screenshot.sh
```

UIはネットワークmountを確実には識別しません。loopback bind、同梱HTTP asset、FFmpeg file protocol、Python API guardは防御層ですが、OS全体のネットワーク遮断や「絶対に送信されない」保証ではありません。WebKitの合成表示では1024pxと200% page zoomで横はみ出しがなく、検索・タブ・候補欄が表示され、検索操作はフォーカス可能でした。macOSのファイル選択dialog、物理的な操作、フル試合性能、ユーザー素材での検索精度は未確認です。

## 実映像を初めて試す前の確認

1. 出力storeを動画の横/Movies内ではなく、十分な空き容量のある明示的なローカル作業フォルダに置く。
2. 依存とモデルを事前準備し、実行中にモデル取得やクラウドAPIを使わないことを確認する。現行MVPはモデル非依存baselineが既定。
3. まずコピーした短い動画1本だけを登録して、パス、時刻、容量、継続時間、再開動作を確認する。
4. SQLiteにはローカル動画パスと特徴量が保存される点を確認し、出力ディスクの暗号化・バックアップ方針を決める。
5. 対象素材の保存/利用権限を確認し、本人が検索例を視聴して有用性を評価する。合成試験を精度根拠にしない。

## 再現テスト

```sh
python -m pytest -q
python -m pytest -q tests/test_ui.py tests/test_core.py
PYTHONPATH=src python -m jepasearch.ui --help
PYTHONPATH=src python3 -m jepasearch.cli runtime
PYTHONPATH=src python3 -m jepasearch.cli bench --output benchmark.json
```


## 参照

- [V-JEPA 2公式モデルカード (固定revision)](https://huggingface.co/facebook/vjepa2-vitl-fpc64-256/tree/b3c1679b7c34d3255ef3547f27c7b226aefab26f)
- [Meta FAIR公式V-JEPA 2 repo](https://github.com/facebookresearch/vjepa2)
