# JEPA-ScenePick

Mac上で動かす、動画の類似区間検索の試作です。2秒の見本区間を入力し、索引済みの動画から似た候補の元動画時刻を探します。候補の前後を再生して比較し、`keep` / `hold` を記録して参照リストをJSONで書き出せます。

これはV-JEPA 2の動画特徴量を使った、一般の動画向け類似区間検索の試作です。動画内容の分類やイベント認識を行うものではなく、類似度は正解確率ではありません。合成動画での動作確認は、実映像で役立つ検索結果が得られる証明ではありません。

## 構成

| 部分 | 技術 | 役割 |
|---|---|---|
| 画面 | vanilla HTML / CSS / JavaScript | 区間選択、検索、候補比較、採否、JSON export |
| ローカルAPIとジョブ | Python標準ライブラリのHTTP server / threading | `127.0.0.1` のHTTP API、画面session、単一ジョブの進捗管理 |
| 動画・検索コア | Python、OpenCV、NumPy | ローカル動画のデコード、2秒窓の特徴抽出、類似候補の順位付け |
| 保存 | SQLite | 動画の索引情報、特徴量、処理状態、採否をローカル保存 |
| V-JEPA 2 | 任意のPyTorch等と外部loader | 固定したローカルcheckpointから特徴を抽出 |
| QA補助 | 任意のSwift / WebKit | 合成画面の撮影、UI操作・表示サイズ・動画再生の確認。通常利用には不要 |

ブラウザはlocalhostのAPIと選択済み動画の再生endpointを使います。API、ジョブ、コア、モデルは1つのPython server process内で動きます。索引・検索は別のモデルserverやprocessを起動しません。画面のHTTPリクエスト用threadとは別に、索引または検索を行うdaemon threadが1つ動き、同時に実行できる重いジョブは1つです。

## まずColor + Motionで試す

macOSとPython 3.12または3.13を用意してください。プロジェクトの対応Pythonは`>=3.12,<3.14`です。V-JEPA 2を使わない検索にはPyTorchやモデルcheckpointは不要です。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

`pip install`はPyPIからNumPyとOpenCVの固定版を取得します。合成動画を作り、Color + Motionで索引・検索する最短例です。全ファイルは`/tmp`に作成します。

```sh
jepa-search synth --output /tmp/jepa-demo.mp4
jepa-search index /tmp/jepa-demo.mp4 --store /tmp/jepa-demo.sqlite --engine color-motion
jepa-search search /tmp/jepa-demo.mp4 --start 0 --end 2 \
  --store /tmp/jepa-demo.sqlite --engine color-motion
```

検索結果には元動画、開始・終了秒、類似度などが含まれます。類似度は正解確率ではありません。結果の`media_id`と`start_s`を使うと採否を記録し、参照一覧を確認できます。

```sh
jepa-search decide --store /tmp/jepa-demo.sqlite \
  --media-id '<検索結果のmedia_id>' --start '<検索結果のstart_s>' --decision keep
jepa-search references --store /tmp/jepa-demo.sqlite
```

`jepa-search synth`は自作図形を使った短い合成動画を生成します。既存ファイルを上書きせず、`Movies`という名前のディレクトリ配下への出力を拒否します。リポジトリ内にも確認用の合成素材 [`samples/synthetic-court-motion.mp4`](samples/synthetic-court-motion.mp4) があります。

## MacローカルUI

インストール後、リポジトリ直下で起動します。

```sh
jepa-search-ui --port 8765
```

画面が開かない場合は`http://127.0.0.1:8765/`をブラウザで開きます。`--no-open`を付けると自動でブラウザを開きません。画面で動画を選び、保存先を決め、利用可能な検索方式で索引を作ります。見本の開始時刻を選び、2秒区間を検索して候補を比較し、採否を記録してJSONをexportします。

索引保存先の既定値は`~/Library/Application Support/jepa-video-search/index.sqlite`です。別の保存先も画面から選べます。停止した索引は同じ動画・保存先・処理fingerprintで再開でき、完了済み窓は再計算を省きます。検索見本は索引窓と同じ2秒である必要があります。JSON参照リストには動画のローカルパスが含まれるため、export先や共有先を確認してください。

### UIが使う方式

- **Color + Motion**はOpenCV CPUによる軽量baselineです。4x4色グリッドと簡単なフレーム差分を特徴にします。動画内容の意味を認識するモデルではありません。
- **V-JEPA 2**は、固定したモデル・前処理・窓設定の特徴量を使います。必要な依存、外部loader、ローカルcheckpoint、checkpoint登録がない場合は利用不可と表示されます。自動で別方式へ切り替わらないため、モデル未準備なら画面でColor + Motionを選んでください。

## V-JEPA 2を使う場合（任意）

V-JEPA 2の推論には、次のすべてを別途用意します。

1. Python 3.12環境。モデル環境の準備スクリプトは3.12を確認します。
2. 指定commitとsource hashに一致する`jepa-risk-detection` loader checkout。これは本リポジトリには含まれません。既定ではこのプロジェクトの一つ上にある`../jepa-risk-detection`を参照します。
3. Metaが案内するraw ViT-L checkpoint。固定した[Meta V-JEPA 2 source README](https://github.com/facebookresearch/vjepa2/blob/204698b45b3712590f06245fbfba32d3be539812/README.md)の配布案内を確認し、`../jepa-risk-detection/artifacts/checkpoints/vitl.pt`に配置します。checkpointは大容量で、本リポジトリには含まず、自動ダウンロードもしません。

loader checkoutとcheckpointを別途用意した後、専用venvと固定依存を準備します。

```sh
./scripts/setup_model_env.sh
```

このスクリプトは既定で`../jepa-risk-detection/.venv/bin/python`をPython 3.12のbase interpreterとして使います。別のPython 3.12を使う場合は`JEPA_VJEPA_BASE_PYTHON`で指定できます。固定依存のインストール時はPyPIへ接続します。モデルcheckpointは取得しません。

モデル依存は次の版に固定されています（[`pyproject.toml`](pyproject.toml)）。

| 用途 | パッケージと固定版 |
|---|---|
| 基本処理 | NumPy 2.2.6、opencv-python-headless 4.11.0.86 |
| V-JEPA 2追加依存 | Pillow 11.2.1、PyTorch 2.7.1、TorchVision 0.22.1、Transformers 4.53.3 |
| テスト追加依存 | pytest 8.4.1 |

checkpointを所定の場所に配置した後、プロジェクトrootから一度だけローカルpinを登録します。

```sh
.venv-vjepa/bin/python scripts/register_local_checkpoint.py \
  --checkpoint ../jepa-risk-detection/artifacts/checkpoints/vitl.pt
```

この登録はファイルのSHA-256をローカル記録し、後続利用時に同じbytesかを確認するものです。提供元の真正性を検証する署名や公式checksumではありません。登録scriptは所定のloader相対パス以外のcheckpointを受け付けず、既存登録の上書きには明示的な`--replace`が必要です。

モデルruntimeでは`jepa-risk-detection`のloaderをimportします。loaderはcommit `4b2c30ab02fd4d951084a5439359c158d31f0c66`と対象source hashに一致する必要があります。checkpointはMeta形式のraw `vitl.pt`、processor設定は同梱configに固定しています。実行時にHugging Face Hubへアクセスするfallbackはありません。任意でloader/checkpointの場所を変更する場合は`JEPA_RISK_ROOT`と`JEPA_RAW_CHECKPOINT`を設定できますが、初回登録scriptは上記既定相対パスを要求します。

準備状態とデバイスを確認し、V-JEPA 2でUIを起動する例です。

```sh
.venv-vjepa/bin/python -m jepasearch.cli runtime
.venv-vjepa/bin/python -m jepasearch.ui --port 8765
```

モデルまたはloaderが欠落している場合は取得せず、理由を表示してV-JEPA 2の処理を止めます。loaderが選ぶ実行deviceは`runtime`出力で確認してください。

## 保存と再開

索引SQLiteには動画のローカルpath、ファイル情報、元動画時刻、特徴量、モデル・前処理・窓のfingerprint、採否が保存されます。元動画自体はコピー・変更しません。storeは動画本体とは別のローカルフォルダに置き、容量に余裕があることを確認してください。索引途中で停止した場合は、同じ保存先と検索方式を選び「索引を作成 / 再開」を使います。UI serverを終了すると画面sessionは消えますが、SQLiteの索引データは残ります。

## データの扱いとプライバシー境界

- UI serverは`127.0.0.1`だけで待ち受けます。動画をブラウザからuploadするAPI、クラウド検索API、telemetry機能はありません。UIは選択済み動画をローカルserverから再生します。
- 索引・検索時にモデルや依存を自動取得しません。依存のインストールは別工程で、PyPIへの通信が発生します。モデルcheckpointの取得も利用者が公式配布案内を確認して別途行います。
- ローカル動画のpath・特徴量・時刻はSQLiteに保存されます。参照リストJSONにもpathが含まれます。出力ディスク、バックアップ、export先を利用者が管理してください。
- この実装はOS全体のネットワークを遮断しません。「どの環境でもデータが絶対に外へ出ない」とは保証しません。Python APIの合成テストで観測した通信範囲も、ネイティブライブラリ、他process、OS全体の通信を証明するものではありません。network mountも確実には判別しません。
- 入力はローカル動画ファイルに制限し、decoderにはFFmpegの`file` protocolを指定します。loopback bindや入力検査は防御層であり、改変された実行環境や同一OS userの任意コードを隔離するものではありません。

## 制約と確認方法

- 検索queryは2秒固定です。類似度は特徴ベクトル間の距離で、正解確率ではありません。Color + Motionは色と単純な動きに依存するbaselineです。
- 合成素材の処理結果は機能確認に限られます。フル試合や大量動画での速度・メモリ、実映像での検索品質、各codecのブラウザ再生は利用環境で確認してください。
- 対応するローカル動画コンテナとブラウザの再生codecは別の条件です。動画を索引できても、ブラウザ上で再生できない場合があります。

テスト追加依存を含めた環境では、合成fixtureを使う自動テストを実行できます。

```sh
python -m pip install -e '.[test]'
python -m pytest -q
python -m jepasearch.cli bench --output /tmp/jepa-benchmark.json
```

任意のmacOS UI QA / screenshot補助にはSwiftとWebKitを使う [`scripts/capture_synthetic_ui_screenshot.sh`](scripts/capture_synthetic_ui_screenshot.sh) があります。通常のUI利用や検索には不要です。

## ライセンス

本プロジェクト独自コードには[MIT License](LICENSE)が適用されます。これは第三者loader、依存パッケージ、モデルcheckpoint、動画、画像などに自動適用されません。各権利条件と未確認事項は[第三者コンポーネント・素材のnotice](THIRD_PARTY_NOTICES.md)を確認してください。これは完全なSBOMや正式な全体監査の代わりではありません。
