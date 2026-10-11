# 第三者コンポーネント・素材の権利情報

このファイルは現行ソースと固定された直接依存に基づく限定的な案内です。完全なSBOM、全体正式監査、法的意見ではありません。JEPA-ScenePickの独自コードには [`LICENSE`](LICENSE) のMIT Licenseが適用され、`pyproject.toml` でも `MIT` と宣言しています。このライセンスは第三者コード、モデル重み、依存物、動画、スクリーンショット、その他のassetへ適用されません。各項目の条件と未確認事項は以下を参照してください。

## V-JEPA 2

- Meta公式V-JEPA 2実装の固定source commit: [`204698b45b3712590f06245fbfba32d3be539812`](https://github.com/facebookresearch/vjepa2/tree/204698b45b3712590f06245fbfba32d3be539812)。そのcommitの [`LICENSE`](https://github.com/facebookresearch/vjepa2/blob/204698b45b3712590f06245fbfba32d3be539812/LICENSE) はMeta Platforms, Inc. and affiliatesを著作権者として記載します。固定commitの[公式README](https://github.com/facebookresearch/vjepa2/blob/204698b45b3712590f06245fbfba32d3be539812/README.md) は大半のcodeをMITとし、3つのdataset utilityのみApache-2.0に分けています。
- このrepoにはMeta公式repo全体や上記3 utilityのソースファイルは含めていません。`src/jepasearch/model.py` は固定形状のV-JEPA encoder出力をpoolするadapterを含みます。adapterとMetaの実装との著作権上の関係はコード作成者が確認し、Meta由来のソースを取り込んでいる場合は該当MITまたはApache noticeを保持してください。
- HFの [`facebook/vjepa2-vitl-fpc64-256`](https://huggingface.co/facebook/vjepa2-vitl-fpc64-256/tree/b3c1679b7c34d3255ef3547f27c7b226aefab26f) 固定revision `b3c1679b7c34d3255ef3547f27c7b226aefab26f` の[モデルカード](https://huggingface.co/facebook/vjepa2-vitl-fpc64-256/blob/b3c1679b7c34d3255ef3547f27c7b226aefab26f/README.md) は `license: mit` と表示します。この表示はそのHF revision内のモデルファイルに関するものです。
- 実行時loaderは外部の `jepa-risk-detection` checkoutから `jepa_risk.features.resolve_device` と `jepa_risk.predictor.load_official_bundle` をimportします。そのcheckoutは本repoに含まず、このrepoの配布物として許諾しません。利用者はそのソースと依存を別途用意する必要があります。
- Meta公式README at the fixed source commitはraw `vitl.pt` へのdownload linkを掲載します。しかしrawファイル自体はGit revisionで固定されず、ファイル固有の再配布ライセンス・checksumも本リポジトリで確認できていません。checkpointは同梱せず自動取得しません。HFカードのMIT表示をraw `.pt` の許諾根拠にしないでください。

## 直接依存

| 目的 | 固定版 | ライセンス情報と対象範囲 |
|---|---|---|
| 数値処理 | NumPy 2.2.6 | BSD-3-Clause。package metadataおよび[version-tagged LICENSE](https://github.com/numpy/numpy/blob/v2.2.6/LICENSE.txt)を参照 |
| 動画処理 | `opencv-python-headless` 4.11.0.86 | OpenCV-Python packaging scriptsはMIT、OpenCV本体はApache-2.0とOpenCV Python projectが説明。公式説明はwheelにFFmpeg (LGPL-2.1) を含み、他のbinary noticeもあるとする。実際に配布するplatform wheelの全構成は別途確認が必要 |
| V-JEPA依存 | Pillow 11.2.1 | MIT-CMU。固定版の[license file](https://github.com/python-pillow/Pillow/blob/11.2.1/LICENSE)を参照 |
| V-JEPA依存 | PyTorch 2.7.1 | BSD-3-Clause。固定版の[license file](https://github.com/pytorch/pytorch/blob/v2.7.1/LICENSE)を参照。wheel内のthird-party componentsは別途確認 |
| V-JEPA依存 | TorchVision 0.22.1 | BSD-3-Clause。固定版の[license file](https://github.com/pytorch/vision/blob/v0.22.1/LICENSE)を参照 |
| V-JEPA依存 | Transformers 4.53.3 | Apache-2.0。固定版の[license file](https://github.com/huggingface/transformers/blob/v4.53.3/LICENSE)を参照 |
| テスト | pytest 8.4.1 | MIT。固定版の[license file](https://github.com/pytest-dev/pytest/blob/8.4.1/LICENSE)を参照 |
| build backend | Hatchling 1.27.0 | MIT。PyPIの[固定版metadata](https://pypi.org/project/hatchling/1.27.0/)を参照 |

これらは `pyproject.toml` に列挙された直接依存です。推移依存のlockfile/SBOMはなく、Python・OS・optional extraによって変わる完全な第三者一覧ではありません。直接依存のlicense表記を、未確認の全wheel収録物に拡張して解釈しないでください。

## OpenCV、FFmpegと外部バイナリ

実行時の動画decodeは `cv2.CAP_FFMPEG` を使います。独立したFFmpeg実行ファイルをrepoへvendoringしていませんが、pipで入れるOpenCV wheelはnative binaryです。OpenCV Python projectの[licensing説明](https://github.com/opencv/opencv-python#licensing)はpackaging scriptsをMIT、OpenCVをApache-2.0、FFmpegをLGPL-2.1と説明し、追加のthird-party license一覧を参照させています。projectで固定したpackage版は [4.11.0.86](https://pypi.org/project/opencv-python-headless/4.11.0.86/) です。

upstreamのlicensing説明は本repoが利用するすべてのplatform-specific wheelを個別識別するrevision付きfile manifestではありません。この確認では実際のmacOS wheelの `LICENSE-3RD-PARTY.txt`、FFmpeg build configuration、codec patent条件を採取・照合していません。ソースrepoだけを公開する場合と、wheelまたはアプリバイナリを再配布する場合を分け、後者は対象wheelのnoticeを同梱して条件を個別確認してください。合成H.264再生成コマンドで利用する外部 `ffmpeg` / `libx264` も同梱していません。

## 素材とモデルの再配布

このrepoには第三者動画、モデル重み、スクリーンショットなどの実素材を同梱しません。独自コードのMIT Licenseはそのような素材の利用・再配布権を与えません。外部動画を利用または再配布する場合は、作品ごとのlicense、作者表示、変更表示、肖像・音源・商標・codec等の別条件を利用者が確認してください。
