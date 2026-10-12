# JEPA-ScenePick

[日本語](README.md)

A prototype for finding similar video segments locally on a Mac. Select a two-second example segment, search indexed videos for similar candidates, review playback around each match, record `keep` or `hold`, and export a JSON reference list.

This prototype uses V-JEPA 2 video features for general-purpose video segment similarity search. It does not classify video content or recognize events, and a similarity score is not a probability of correctness. Successful operation on synthetic videos does not show that useful matches will be found in real footage.

## Architecture

| Component | Technology | Role |
|---|---|---|
| Interface | vanilla HTML / CSS / JavaScript | Segment selection, search, candidate review, decisions, and JSON export |
| Local API and jobs | Python standard-library HTTP server / threading | HTTP API bound to `127.0.0.1`, UI sessions, and single-job progress management |
| Video and search core | Python, OpenCV, NumPy | Local video decoding, two-second feature extraction, and candidate ranking |
| Storage | SQLite | Local index metadata, features, processing state, and decisions |
| V-JEPA 2 | Optional PyTorch and external loader | Feature extraction from a fixed local checkpoint |
| QA helpers | Optional Swift / WebKit | Synthetic UI screenshots, interaction, viewport, and playback checks; not required for normal use |

The browser uses the localhost API and a local playback endpoint for the selected video. The API, jobs, core, and model run in a single Python server process. Indexing and search do not start a separate model server or process. One daemon thread handles indexing or search separately from HTTP request threads, and only one heavy job can run at a time.

## Try the Color + Motion baseline

Use macOS with Python 3.12 or 3.13. The project supports Python `>=3.12,<3.14`. PyTorch and a model checkpoint are not needed for Color + Motion search.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

`pip install` downloads pinned NumPy and OpenCV versions from PyPI. The shortest path to generate a synthetic video and index and search it with Color + Motion is below. All files are created under `/tmp`.

```sh
jepa-search synth --output /tmp/jepa-demo.mp4
jepa-search index /tmp/jepa-demo.mp4 --store /tmp/jepa-demo.sqlite --engine color-motion
jepa-search search /tmp/jepa-demo.mp4 --start 0 --end 2 \
  --store /tmp/jepa-demo.sqlite --engine color-motion
```

Search results include the source video, start and end times in seconds, and similarity scores. A score is not a probability of correctness. Use a result's `media_id` and `start_s` to record a decision and view the reference list.

```sh
jepa-search decide --store /tmp/jepa-demo.sqlite \
  --media-id '<media_id from a search result>' --start '<start_s from a search result>' --decision keep
jepa-search references --store /tmp/jepa-demo.sqlite
```

`jepa-search synth` creates a short video made from original geometric shapes. It refuses to overwrite an existing file and refuses output under a directory named `Movies`. The repository also includes the synthetic fixture [`samples/synthetic-court-motion.mp4`](samples/synthetic-court-motion.mp4).

## Local Mac UI

After installation, start the UI from the repository root:

```sh
jepa-search-ui --port 8765
```

If a browser does not open automatically, visit `http://127.0.0.1:8765/`. Add `--no-open` to disable automatic browser launch. In the UI, select a video, choose a storage location, and create an index with an available search method. Select a two-second example, search for matching segments, review candidates, record decisions, and export a JSON reference list.

The default index path is `~/Library/Application Support/jepa-video-search/index.sqlite`. The UI also lets you choose another path. An interrupted index can resume when the video, store, and processing fingerprint are unchanged; completed windows are skipped. The search example must be two seconds long, matching the indexed windows. The exported JSON reference list contains local video paths, so check its destination and who can access it before sharing.

### Search methods available in the UI

- **Color + Motion** is a lightweight OpenCV CPU baseline. It uses a 4×4 color grid and simple frame differences. It does not recognize video semantics.
- **V-JEPA 2** uses features with fixed model, preprocessing, and window settings. The required dependencies, external loader, local checkpoint, and checkpoint registration must all be available. If any is missing, the method is shown as unavailable; it does not silently switch methods. Select Color + Motion in the UI if the model is not ready.

## Optional V-JEPA 2 setup

The V-JEPA 2 path is optional and is not a turnkey model installation. You must separately prepare all of the following:

1. A Python 3.12 environment. The model environment setup script checks for Python 3.12.
2. A `jepa-risk-detection` loader checkout matching the pinned commit and source hash. The loader is not included in this repository. By default, the project looks for it at `../jepa-risk-detection`.
3. Meta's raw ViT-L checkpoint. Follow the distribution instructions in the pinned [Meta V-JEPA 2 source README](https://github.com/facebookresearch/vjepa2/blob/204698b45b3712590f06245fbfba32d3be539812/README.md) and place it at `../jepa-risk-detection/artifacts/checkpoints/vitl.pt`. It is large, is not included in this repository, and is never downloaded automatically.

After preparing the loader checkout and checkpoint, prepare the dedicated virtual environment and pinned dependencies:

```sh
./scripts/setup_model_env.sh
```

By default, this script uses `../jepa-risk-detection/.venv/bin/python` as its Python 3.12 base interpreter. Set `JEPA_VJEPA_BASE_PYTHON` to use another Python 3.12 interpreter. Installing the pinned dependencies connects to PyPI; the script does not fetch the model checkpoint.

The model dependencies are pinned as follows (see [`pyproject.toml`](pyproject.toml)):

| Purpose | Package and pinned version |
|---|---|
| Core processing | NumPy 2.2.6, opencv-python-headless 4.11.0.86 |
| Additional V-JEPA 2 dependencies | Pillow 11.2.1, PyTorch 2.7.1, TorchVision 0.22.1, Transformers 4.53.3 |
| Test extra | pytest 8.4.1 |

After placing the checkpoint at the required location, register the local pin once from the project root:

```sh
.venv-vjepa/bin/python scripts/register_local_checkpoint.py \
  --checkpoint ../jepa-risk-detection/artifacts/checkpoints/vitl.pt
```

Registration records the file's SHA-256 locally and checks that later use has the same bytes. It does not verify the publisher's authenticity with a signature or official checksum. The registration script accepts only the expected loader-relative checkpoint path; replacing an existing registration requires the explicit `--replace` option.

At runtime, the model adapter imports the loader from the external `jepa-risk-detection` checkout. That checkout must match commit `4b2c30ab02fd4d951084a5439359c158d31f0c66` and the expected source hash. The checkpoint must be Meta's raw `vitl.pt` format, and the processor settings are fixed to the bundled configuration. There is no Hugging Face Hub fallback at runtime. You can optionally set `JEPA_RISK_ROOT` and `JEPA_RAW_CHECKPOINT` to change the loader and checkpoint locations, but the initial registration script requires the default relative path above.

After preparing the model, check its status and device, then start the UI with V-JEPA 2:

```sh
.venv-vjepa/bin/python -m jepasearch.cli runtime
.venv-vjepa/bin/python -m jepasearch.ui --port 8765
```

If the model or loader is missing, the application reports why and stops V-JEPA 2 processing without downloading anything. Check the device selected by the loader in the `runtime` output.

## Storage and resume

The index SQLite database stores the video's local path, file information, source timestamps, features, model/preprocessing/window fingerprints, and decisions. The original video is not copied or modified. Store the index in a local folder separate from the video and make sure it has enough free space. If indexing is interrupted, select the same store and search method, then choose **Create / Resume Index**. Closing the UI server ends the UI session, but SQLite index data remains.

## Data handling and privacy boundaries

- The UI server listens only on `127.0.0.1`. There is no browser video-upload API, cloud search API, or telemetry feature. The UI plays the selected video from the local server.
- Indexing and search do not automatically download models or dependencies. Dependency installation is a separate step and communicates with PyPI. Users obtain model checkpoints separately after reviewing the official distribution instructions.
- Local video paths, features, and timestamps are stored in SQLite. The JSON reference list also contains paths. Users are responsible for managing the output disk, backups, and export destinations.
- This application does not block networking for the operating system as a whole. It does not guarantee that data can never leave every possible environment. Communications observed in synthetic Python API tests do not prove the behavior of native libraries, other processes, or the operating system as a whole. Network-mounted storage cannot be identified reliably either.
- Inputs are restricted to local video files, and the decoder is configured to use FFmpeg's `file` protocol. Loopback binding and input checks are defensive layers, not isolation from a modified runtime or arbitrary code running as the same OS user.

## Limitations and checks

- Search queries use fixed two-second windows. Similarity is a distance between feature vectors, not a probability of correctness. Color + Motion is a baseline based on color and simple motion.
- Synthetic fixtures validate functionality only. They do not establish search quality on real footage, or speed and memory use on full games or large video collections. Check real-world search quality and browser playback for each codec in your own environment.
- Local video container support and browser playback codec support are separate. A video may be indexable but not playable in the browser.

In an environment with the test extra installed, you can run the automated tests using synthetic fixtures:

```sh
python -m pip install -e '.[test]'
python -m pytest -q
python -m jepasearch.cli bench --output /tmp/jepa-benchmark.json
```

Optional macOS UI QA and screenshot helpers use Swift and WebKit. See [`scripts/capture_synthetic_ui_screenshot.sh`](scripts/capture_synthetic_ui_screenshot.sh). They are not needed for normal UI use or search.

## License

The project's original code and these project-created synthetic videos are covered by the [MIT License](LICENSE):

- [`samples/synthetic-court-motion.mp4`](samples/synthetic-court-motion.mp4)
- [`samples/synthetic-court-motion-h264.mp4`](samples/synthetic-court-motion-h264.mp4)

The standard MIT copyright-notice and permission-notice conditions also apply to these videos. This license does not apply to third-party loaders, dependencies, model checkpoints, real-world videos, or third-party assets. See the [third-party components and assets notice](THIRD_PARTY_NOTICES.md) for applicable terms and known limitations. This notice is not a complete SBOM or formal audit of all components.
