# JEPA-ScenePick (Mac ローカル試作)

短い見本区間から、ローカル動画群の似た時刻を探すためのJEPA試用成果です。今回は実映像を使わず、OpenCVで作った色付き図形の合成動画だけで実装の動作を確認します。検索結果には元動画のパスと秒単位の開始・終了時刻を返し、`keep` / `hold` をengine別に記録して参照リストを作れます。画面は「動画を追加して索引を準備」→「見本区間を選んで検索」→候補比較の順に進み、索引準備後は設定を畳んで区間検索を前面に表示します。

独自プロジェクトコードは [MIT License](LICENSE) で許諾し、package metadataにも `MIT` と宣言しています。この許諾は独自コードだけに適用され、第三者コード、モデル重み、依存物、動画、スクリーンショット、その他のassetへは拡張されません。それらには個別の権利条件が適用されます。直接依存とモデル素材の未確認事項は[第三者notice](THIRD_PARTY_NOTICES.md)に記載しています。

## プライバシー境界

- このリポジトリに含める動画は自作図形の合成サンプルだけです。実動画、索引DB、埋め込み、モデルcheckpoint、依存環境、利用者データは同梱しません。
- 本開発・検証では実映像、音声、フレーム、サムネイル、利用者の個人動画フォルダを使わず、自作図形の合成素材だけを使いました。再現テストが生成する素材とDBは一時ディレクトリに作成します。合成ジェネレータは個人動画フォルダ配下と既存ファイルへの出力を拒否し、上書きしません。
- 検索・索引処理にHTTP/API/テレメトリ送信機能はありません。V-JEPA processorは同梱JSONから作成し、raw checkpointを指定済みローカルパスから元loaderで開きます。HF Hubモデルsnapshotへのアクセスやfallbackはありません。欠落時はエラー停止します。依存インストールも実行時処理から分離しています。
- これはコード経路の確認であり、OS全体のネットワーク遮断や「絶対に送信されない」保証ではありません。合成CLIテストでは子Pythonプロセスの `socket.connect` / `create_connection` / `urllib.request.urlopen` を例外化し、その条件下で生成・索引・検索・採否・参照リストまで成功しました。これはPythonレベルのこれらAPIに限った観測です。OpenCV/FFmpegのネイティブコード、他プロセスやOS全体の通信は捕捉せず、外部通信の絶対不在や独立した通信監査を証明しません。
- 索引SQLiteには、利用者が後日指定する動画のローカルパス、サイズ、更新時刻、特徴量、元動画時刻が保存されます。ファイル名やパスは外部へ送る設計ではありません。現在のbaseline特徴量は4x4色グリッドの平均と単純な動き量です。

## 準備・実行

動作対象は Python 3.12/3.13、macOS arm64。依存の版は `pyproject.toml` と `requirements-model.txt` に固定しています。クリーンなPython 3.12環境でbaseline依存を導入する例です。コマンドはリポジトリ直下で実行します。

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

上記はクリーン環境向けの導入例で、今回この手順による新規venvへのbaseline依存インストールは検証していません。今回の導入確認は既存環境での `python3 -m jepasearch.cli --help` と `python3 -m jepasearch.ui --help` の表示だけです。これは新規導入、動画処理、全コマンドの動作保証ではありません。

検索結果に返る `media_id` と `start_s` を指定して採否を記録します。

```sh
PYTHONPATH=src python -m jepasearch.cli decide --store /tmp/jepa-demo.sqlite --media-id '<media_id>' --start 0 --decision keep
PYTHONPATH=src python -m jepasearch.cli references --store /tmp/jepa-demo.sqlite
PYTHONPATH=src python -m jepasearch.cli bench --output benchmark.json
```

### V-JEPA 2のローカル準備

UIの初期選択はV-JEPA 2です。利用可能な場合、状態欄には「V-JEPA 2：利用可能」と表示し、詳細な準備・検証説明は閉じた「モデル詳細・検証状態」に置きます。準備が不足している場合は理由と必要な操作を状態欄に表示し、V-JEPA 2での索引・検索は開始できません。Color + Motionへ切り替えればbaselineを使えます。engineは自動で切り替わりません。

V-JEPA 2を使うには、`model` extraの固定依存、固定commitのloader、ローカルraw ViT-L checkpoint、および明示的な初回ローカル登録が必要です。TorchVision 0.22.1はPyTorch 2.7.1に対応するmacOS組み合わせとして[公式version table](https://pytorch.org/get-started/previous-versions/)に掲載されています。UIのreadinessは登録記録、依存版、loader/source pinを確認しますが、重みbytesを読みません。実際のSHA-256照合はモデルload前に行います。

既存のモデル環境を変更しないproject専用venvは次で準備します。固定packageは公式PyPIから取得します。このコマンドはモデル重みを取得しません。

```sh
./scripts/setup_model_env.sh
```

既定checkpointは隣接する既存 `jepa-risk-detection/artifacts/checkpoints/vitl.pt` です。既存READMEはViT-L公式checkpointとしてこの場所とローカルファイルサイズを記録していますが、個別ファイルの公式署名やSHA-256は記録していません。既存ファイルの再利用が承認された場合、次のコマンドはSHA-256をproject内 `.local/checkpoint-identity.json` に登録します。これは今後の同じローカルファイルとの整合性を確認するpinで、提供元の真正性を証明せず、公式checksum/署名検証とも異なります。記録はgitignore対象で、明示的な `--replace` がない限り上書きしません。UIの短い成功表示も公式の真正性確認を意味しません。この説明は実装経路の案内であり、独立したセキュリティ監査結果ではありません。

```sh
.venv-vjepa/bin/python scripts/register_local_checkpoint.py \\
  --checkpoint /path/to/jepa-risk-detection/artifacts/checkpoints/vitl.pt
```

```sh
.venv-vjepa/bin/python -m jepasearch.cli runtime
.venv-vjepa/bin/python -m jepasearch.cli index /path/to/video.mp4 --store /path/to/index.sqlite --engine vjepa2
```

`JEPA_RISK_ROOT` は許可を得て用意したloader checkoutを指し、`src/jepa_risk/features.py` と `src/jepa_risk/predictor.py` を含む必要があります。このprojectは固定した既存sourceを実行時にimportしますが、同梱もPyPI配布もしていません。`JEPA_RAW_CHECKPOINT` はMeta形式のViT-L raw checkpointを指します。既定位置はloader checkoutの `artifacts/checkpoints/vitl.pt` です。現在のPython環境の依存が固定版と一致しない場合もV-JEPAは利用不可として表示されます。

checkpointは自動取得せず、repositoryにも含めません。[Metaの固定source commit README](https://github.com/facebookresearch/vjepa2/blob/204698b45b3712590f06245fbfba32d3be539812/README.md) はViT-L/16 checkpointを列挙し配布リンクを案内しますが、SHA-256 checksumは掲載していません。ローカルhashは既存ファイルの同一性だけを識別し、提供元の真正性を証明しません。初回登録記録は既存ファイルを利用する判断とその出所説明を残し、後続起動でのbyte整合性確認はhashで行います。重みのミラー・再配布は権利確認が済むまで行わないでください。

V-JEPA実行にはproject専用`.venv-vjepa`と、別途用意した固定loader/checkpointが必要です。起動スクリプトはそのvenvを使い、他の環境設定を変更しません。loaderと重みはこのrepoに同梱せず、取得・利用条件も個別に確認してください。

同じ索引への再登録は既存windowを飛ばして再開します。`--no-resume` は登録済み動画を拒否します。同じstoreに対する同時登録はロックで拒否し、容量が推定必要量+256 MiBを下回ればSQLite接続前に停止します（出力親ディレクトリは必要なら作成しますが、DB/sidecarは作りません）。storeのsymlink（danglingを含む）と元動画の同一パス・同一inodeへの出力は拒否します。index後に動画が更新されたことも検出します。近接結果は同動画内1秒以内を抑制します。モデル (`none-baseline-v1`)、前処理 (`rgb-grid-4x4-motion-v1`)、窓 (`2s-stride-0.5s-samples-8-terminal-2frame-retry-v2`) はSQLiteのwindow行で個別に記録し、複合主キーにも含めます。さらにengine/model/preprocess/window/stride/sample count/feature dtype/device/poolingのfingerprintをpipeline IDへ組み込み、再開・検索・採否はそのIDと現在有効なindexを照合します。設定変更後は別fingerprintへ索引され、特徴を混ぜません。決定レコードもpipeline別に保存します。秒位置はOpenCVが報告するFPSとframe countから計算しており、可変フレームレートや放送タイムコードの精度は未検証です。終端側の半秒窓だけ、最終フレームへのseekに失敗した場合は末尾サンプル点をもう1フレーム前へずらして一度再試行します。窓サンプリング版は`2s-stride-0.5s-samples-8-terminal-2frame-retry-v2`とV-JEPA用`2s-stride-0.5s-fpc64-terminal-2frame-retry-v2`に更新し、旧特徴とはfingerprintを分離します。JEPA特徴は別model/preprocess/window IDで保存され、既存baseline特徴と混ざりません。

## 索引移行とローカル資源上限

同一動画のengine別索引状態はSQLite schema version 2の`media_pipeline_state`で管理します。旧DBの`media`・`windows`・`decisions`行は削除せず、schema移行をtransaction内で行います。旧DBで現在のmedia行と異なるpipelineのwindowは、過去の完了状態を復元できないため`interrupted`として残します。検索対象へ戻すには、そのengineで再開して不足windowを埋めてください。DB・lock・存在するWAL/SHMはowner-only (0600) に制限します。

UIは最大16 HTTP worker、32 session (12時間で期限切れ)、sessionあたり最大512ファイル/1 TiB、1ファイル200 GiB、1 job 32ファイルに制限します。動画は最大12時間・3840×2160、検索は最大50,000 windows/500候補です。上限を超える素材は明示エラーとなります。索引jobの12時間制限はwindow境界で協調停止します。検索処理は強制中断せず、完了時に12時間超なら結果を破棄します。native decoder自体をsandbox化する保証ではありません。

## V-JEPA 2の実装境界

実装で固定しているのは Meta V-JEPA 2 source commit `204698b45b3712590f06245fbfba32d3be539812` とHF ViT-L processor/config revision `b3c1679b7c34d3255ef3547f27c7b226aefab26f` です。processor設定は小さなJSONとして同梱します。重みはHF snapshotではなく、別途用意されたMeta形式の `vitl.pt` を外部loaderで読む実装です。実行時にHF Hubからモデルを取得するfallbackはありません。

ローカルloaderはcommit `4b2c30ab02fd4d951084a5439359c158d31f0c66`、cleanな`src/jepa_risk` source tree、および`predictor.py`/`features.py`のSHA-256に一致する必要があります。PyTorch 2.7.1、TorchVision 0.22.1、Transformers 4.53.3、Pillow 11.2.1以外のmodel runtimeは拒否します。登録済みローカルpinがある場合、`JEPA_RAW_CHECKPOINT_SHA256`は省略できます。設定された場合は登録pinと一致しなければ拒否します。実モデルのロードは`torch.load(weights_only=True)`を使う既存pinned loader経由に限定し、pickle code-execution fallbackを行いません。

このrepoはMetaの公式ソースツリーも外部loaderも同梱しません。READMEのローダー境界に記載したとおり、`jepa-risk-detection` checkoutの `load_official_bundle()` と `resolve_device()` を実行時importします。コード作成者はadapterがMeta公式ソースの著作権上の派生物に当たるかを公開前に確認してください。Meta公式READMEは固定commitにおける大半のcodeをMIT、3つのdataset utilityをApache-2.0と分けていますが、これだけではraw checkpointの権利を確定しません。

公式Meta READMEはraw `vitl.pt` の取得先を案内しますが、ファイルそのものに固定revision・チェックサム・個別再配布条件はこのrepoで確認できていません。HFモデルカードのMIT表記は固定HF model revisionの表示です。raw checkpointの再配布許可を意味しません。重みと依存の条件は[第三者notice](THIRD_PARTY_NOTICES.md)を参照してください。

検索queryの区間長は索引窓と一致する2秒のみ受け付けます。許容誤差は±0.000001秒（1µs）で、これを外れる区間や有限値でない時刻は動画を開く前にエラーで拒否します。V-JEPAの入力は64 RGB frames / 2 seconds、公式processorのshortest-edge 292 resizeと256x256 center crop、32x16x16のViT token mean poolingで1024次元です。色/動きbaselineとはmodel, preprocessing, window versionを別々に記録し、検索対象もpipeline別です。索引メタデータの動画パスはローカルSQLiteに保存されます。URL・RTSP/TCP/file URI・UNC・playlist/link拡張子・playlist内容を動画として渡すのは拒否します。拡張子だけでなくMP4/MOV/AVI/Matroska/MPEG/TSの許可署名を照合してから、OpenCVのFFmpeg backendへ渡します。decoder open時はFFmpeg `protocol_whitelist=file` を指定します。これは入力境界の防御で、OS全体の遮断や任意のnative backend/OS経路に対する絶対保証ではありません。明示パスがネットワークmountを指すかの完全判定までは行いません。

## 合成ベンチマークと限界

合成素材によるローカルsmoke testでは、V-JEPA 2で1つの2秒窓を索引して自己検索候補を返し、別の合成H.264 UI確認では候補比較・採否・参照出力を操作しました。これは限定的な機能確認であり、性能や検索精度の検証・保証ではありません。


合成素材の結果は接続確認と負荷1回の記録であり、バスケ放送映像やヘルメットPOVスキー映像での検索品質を証明しません。近接重複抑制、VFR/放送タイムコード、長時間の停止再開や容量見積もりの精度、フル試合の速度/メモリ、CPU fallback、mounted filesystem判定、別依存版での再現性は未検証です。システム全体のネットワーク遮断を設定していません。基礎CLIのネットワークガード試験と合成offline実行は通りましたが、Pythonの監視対象外となるネイティブコード・他プロセス・OSレベル通信の絶対的不在までは主張しません。

このリポジトリの84件の自動テストは合成fixtureを使った機能・境界確認です。実映像の検索品質、フル試合や大量素材の処理時間・メモリ、長時間の停止再開、CPU fallback、別依存版での再現性は未検証です。

## 再現方法

このMacでは次でraw checkpointを使った合成の索引・検索・採用・参照リストを再実行できます。既存venvを変更せず、project専用venvとHF offline環境変数を使い、一時ディレクトリに合成動画とDBを作ります。

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-vjepa/bin/python scripts/vjepa_synthetic_integration.py
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

`open_ui_server.command` はオフライン環境変数と専用venvを使って8767番でユーザー起動のバックグラウンドprocessを開始します。起動前に固定依存と`.local/checkpoint-identity.json`を準備状態だけで確認し、重みを読み込みません。ログは保存せず、OSの常駐設定も追加しません。8766番など指定portが使用中なら何も停止せずエラーになります。管理対象の既存serverが異なるPython環境で動いている場合はそのまま残し、明示的に停止してから再起動する案内を表示します。

```sh
JEPA_UI_PORT=8767 sh scripts/manage_ui_server.sh start
JEPA_UI_PORT=8767 sh scripts/manage_ui_server.sh status
# 終了時
JEPA_UI_PORT=8767 sh scripts/manage_ui_server.sh stop
```

Terminalを直接開く場合は `open -a Terminal scripts/open_ui_server.command` を実行するか、この `.command` をFinderから起動できます。8766番の以前のprocess/sessionはこの起動操作では停止・変更しません。

URLは `http://127.0.0.1:8767/` です。同梱UIの静的表示を更新したときは、ブラウザページを再読込すると反映します。これはserver processの再起動ではありません。同じprocessが動作中で有効なsession cookieを使う通常の再読込では、選択やjobを保持したsessionを再利用します。serverを終了・再起動するとprocess内の動画選択とsession/job状態は失われます。索引データは既存の保存先に残り、同じ動画と保存先を選んで索引を再開できます。

「動画を選択」と「保存先を選ぶ」はmacOSのネイティブdialogです。ファイルは1本ずつ選び、追加で繰り返せます。ブラウザからの動画uploadや任意pathを読むAPIはなく、選択pathは同じローカルprocess内に保管し、HTTPにはsession所有のopaque file IDだけを使います。選んだ素材と保存先はlocalhost上の同一sessionに束縛されます。サーバーが動作している間は画面を閉じて再接続しても、同じsession cookieがあればjob状況へ戻れます。serverを終了・再起動した場合は動画を選び直し、同じ索引保存先で「索引を作成 / 再開」を押すと既存fingerprintの窓から続きます。

索引中はモデル起動と窓単位の進捗を表示します。停止は処理中の1窓を終えた後に反映され、次の再開は完了済み窓を飛ばします。同時jobは一つに制限し、別のsessionからは停止できません。検索見本は索引窓と一致する2秒（±1µs）だけ受理します。検索結果では元動画の開始・終了時刻、特徴類似度、結果を生成したエンジンとfeature fingerprintを表示し、類似度は正解確率ではないと明示します。検索エンジンを切り替えた後は、既存候補を生成した方式との違いを表示し、新しい検索まで結果が前の方式のままであることを示します。候補プレビューと見本を並べて比べ、2秒区間と前後各5秒の文脈を切替再生し、採用/保留を記録できます。JSON参照リストはブラウザの標準download先に保存されます。参照JSONには索引元のローカルpathが含まれるので、保存先と共有範囲を利用者が確認してください。

server bindはIPv4 `127.0.0.1` 固定です。Hostをport込みで照合し、状態変更APIにはOrigin、session cookie、CSRF tokenが必要です。APIにpath値を送信できず、再生は選択済みopaque ID、単一Range、選択時と再生直前のファイル署名・inode照合を通ります。decoderは既存coreの許可containerとFFmpeg `file` protocolだけを使います。静的assetは同梱しCSPで外部originを拒否します。出力databaseは既定で `~/Library/Application Support/jepa-video-search/index.sqlite` に置きます。空き容量不足はSQLite作成前に止め、sourceとの同一inode、dangling symlinkを拒否します。

WebKitで表示した画面と操作は合成fixtureだけで確認できます。キャプチャ用runnerは検索、タブ矢印移動、前後区間の切替、候補比較、重複した採用操作、エンジン変更後の結果由来表示をDOMイベントで確認します。さらに1024px幅と200% page zoomで横はみ出し、主要パネル、検索ボタンの視認・フォーカス可能性を確認します。これは物理的なマウス/キーボード操作や、画面上での再生品質の手動確認ではありません。test_ui.py はHTTP API/画面構造のテストです。再現コマンドは合成fixtureと一時索引だけを使います。

動画は索引デコーダーで扱えても、ブラウザがそのcodecを再生できるとは限りません。MPEG-4 Part 2の合成動画はWebKitで再生でき、Chrome 154では再生できませんでした。H.264 Baselineの合成版はChrome 154で再生できました。再生error時はプレーヤー上に形式/codecの案内を表示します。実映像評価前に、利用予定のブラウザで試してください。

```sh
scripts/capture_synthetic_ui_screenshot.sh
```

UIはネットワークmountを確実には識別しません。loopback bind、同梱HTTP asset、FFmpeg file protocol、Python API guardは防御層ですが、OS全体のネットワーク遮断や「絶対に送信されない」保証ではありません。ページ初期化は`Sec-Fetch-Site`が`same-origin`または`none`であることを要求し、存在する`Origin`も検査します。Fetch Metadataを送らない古いclientは拒否されます。管理起動スクリプトはPID file path構築前にport値を1〜65535の10進数として検証します。スケール診断はprivate入力をignored local stateの`private` subtreeに制限し、public manifest pathも解決後に設定root内であることを確認します。path確認後に同じlocal directoryを変更できるprocessとの競合までは防ぎません。これらは同一OS userの任意コードや改変済みローカル実行環境を防ぐOS境界ではありません。WebKitの合成表示では1024pxと200% page zoomで横はみ出しがなく、検索・タブ・候補欄が表示され、検索操作はフォーカス可能でした。macOSのファイル選択dialog、物理的な操作、フル試合性能、ユーザー素材での検索精度は未確認です。

## 実映像を初めて試す前の確認

1. 出力storeを動画本体と同じフォルダではなく、十分な空き容量のある明示的なローカル作業フォルダに置く。
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
