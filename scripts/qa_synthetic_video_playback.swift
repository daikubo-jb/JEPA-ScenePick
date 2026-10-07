import AppKit
import Foundation
import WebKit

final class PlaybackProbe: NSObject, WKNavigationDelegate {
    let webView: WKWebView
    var finished = false
    var failed = false

    init(_ webView: WKWebView) { self.webView = webView }

    private func finishWithValue(_ value: Any?) {
        guard let raw = value as? String, let data = raw.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let query = json["query"] as? [String: Any],
              let candidate = json["candidate"] as? [String: Any] else {
            print("PLAYBACK_PROBE_RESULT_MISSING \(value ?? "null")")
            failed = true
            finished = true
            return
        }
        func worked(_ item: [String: Any]) -> Bool {
            let time = item["currentTime"] as? Double ?? 0
            let frames = item["webkitDecodedFrames"] as? Int ?? item["decodedFrames"] as? Int ?? 0
            let width = item["videoWidth"] as? Int ?? 0
            let events = item["events"] as? [String] ?? []
            return time > 0.2 && (frames > 0 || events.contains("loadeddata") || events.contains("playing")) && width > 0
        }
        print("PLAYBACK_PROBE \(raw)")
        if !worked(query) || !worked(candidate) { failed = true }
        finished = true
    }

    private func pollResult(_ attempts: Int = 30) {
        webView.evaluateJavaScript("window.__playbackResult || null") { value, error in
            if let error = error {
                print("PLAYBACK_PROBE_ERROR \(error.localizedDescription)")
                self.failed = true
                self.finished = true
            } else if value != nil && !(value is NSNull) {
                self.finishWithValue(value)
            } else if attempts > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.pollResult(attempts - 1) }
            } else {
                print("PLAYBACK_PROBE_TIMEOUT")
                self.failed = true
                self.finished = true
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let js = #"""
        (() => {
        (async () => {
          const query = document.querySelector('#query-player');
          const compare = document.querySelector('.match-card .button-compare');
          const candidate = document.querySelector('#candidate-player');
          for (let i = 0; i < 40; i++) {
            if (query.currentSrc && query.readyState >= 2 && document.querySelector('.match-card')) break;
            await new Promise(resolve => setTimeout(resolve, 150));
          }
          const inspect = async (video) => {
            const events = [];
            for (const name of ['loadedmetadata','loadeddata','canplay','playing','timeupdate','error']) {
              video.addEventListener(name, () => events.push(name), { once: name !== 'timeupdate' });
            }
            let playOutcome = 'pending';
            try {
              const result = await Promise.race([
                video.play().then(() => 'resolved', error => `rejected:${error.name}:${error.message}`),
                new Promise(resolve => setTimeout(() => resolve('timeout'), 3500))
              ]);
              playOutcome = result;
              await new Promise(resolve => setTimeout(resolve, 1400));
            } catch (error) { playOutcome = `threw:${error.name}:${error.message}`; }
            let frames = null;
            try { frames = video.getVideoPlaybackQuality().totalVideoFrames; } catch {}
            const webkitFrames = Number.isFinite(video.webkitDecodedFrameCount) ? video.webkitDecodedFrameCount : null;
            const result = {
              playOutcome, events, currentTime: video.currentTime, duration: video.duration,
              readyState: video.readyState, networkState: video.networkState,
              videoWidth: video.videoWidth, videoHeight: video.videoHeight,
              errorCode: video.error?.code ?? null, errorMessage: video.error?.message ?? null,
              decodedFrames: frames, webkitDecodedFrames: webkitFrames, paused: video.paused
            };
            video.pause();
            return result;
          };
          document.querySelector('#query-view-tab').click();
          const queryResult = await inspect(query);
          compare?.click();
          const candidateResult = await inspect(candidate);
          window.__playbackResult = JSON.stringify({
            source: query.currentSrc,
            mp4vCanPlay: query.canPlayType('video/mp4; codecs="mp4v.20.3"'),
            h264CanPlay: query.canPlayType('video/mp4; codecs="avc1.42E01E"'),
            query: queryResult, candidate: candidateResult
          });
        })().catch(error => { window.__playbackResult = JSON.stringify({ harnessError: `${error.name}:${error.message}` }); });
        return 'probe-started';
        })()
        """#
        webView.evaluateJavaScript(js) { value, error in
            if let error = error {
                print("PLAYBACK_PROBE_ERROR \(error.localizedDescription)")
                self.failed = true
                self.finished = true
                return
            }
            self.pollResult()
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        print("PLAYBACK_NAVIGATION_ERROR \(error.localizedDescription)")
        failed = true
        finished = true
    }
}

guard CommandLine.arguments.count == 2,
      let url = URL(string: CommandLine.arguments[1]), url.scheme == "http",
      url.host == "127.0.0.1", url.port != nil else {
    fputs("usage: qa_synthetic_video_playback.swift http://127.0.0.1:PORT/\n", stderr)
    exit(2)
}

NSApplication.shared.setActivationPolicy(.accessory)
let config = WKWebViewConfiguration()
config.websiteDataStore = .nonPersistent()
config.mediaTypesRequiringUserActionForPlayback = []
let view = WKWebView(frame: CGRect(x: 0, y: 0, width: 1280, height: 900), configuration: config)
let window = NSWindow(contentRect: view.frame, styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
window.title = "Synthetic Video Playback QA"
window.contentView = view
window.center()
window.makeKeyAndOrderFront(nil)
NSApplication.shared.activate(ignoringOtherApps: true)
let probe = PlaybackProbe(view)
view.navigationDelegate = probe
view.load(URLRequest(url: url))
let deadline = Date().addingTimeInterval(15)
while !probe.finished && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}
if !probe.finished {
    print("PLAYBACK_PROBE_TIMEOUT")
    probe.failed = true
}
exit(probe.failed ? 1 : 0)
