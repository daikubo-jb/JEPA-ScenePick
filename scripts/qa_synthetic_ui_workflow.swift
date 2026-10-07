import AppKit
import Foundation
import WebKit

final class WorkflowResultBridge: NSObject, WKScriptMessageHandler {
    var onResult: ((Any) -> Void)?
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        onResult?(message.body)
    }
}

final class WorkflowQA: NSObject, WKNavigationDelegate, WKDownloadDelegate {
    let webView: WKWebView
    let downloadPath: URL
    var finished = false
    var failed = false
    var pageResult: [String: Any]?
    var exportVerified = false
    var didPrintPageResult = false

    init(webView: WKWebView, downloadPath: URL) {
        self.webView = webView
        self.downloadPath = downloadPath
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let js = #"""
        (() => {
          const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
          const waitFor = async (fn, label, timeout = 120000) => {
            const end = Date.now() + timeout;
            while (Date.now() < end) {
              const value = fn();
              if (value) return value;
              await pause(150);
            }
            throw new Error(`timeout:${label}`);
          };
          const playback = async video => {
            await waitFor(() => video.currentSrc && video.readyState >= 2, 'video ready', 15000);
            const before = video.currentTime;
            const events = [];
            for (const event of ['loadeddata','playing','timeupdate','error']) {
              video.addEventListener(event, () => events.push(event), { once: event !== 'timeupdate' });
            }
            const result = await Promise.race([
              video.play().then(() => 'resolved', e => `rejected:${e.name}:${e.message}`),
              pause(5000).then(() => 'timeout')
            ]);
            await pause(900);
            const state = {
              play: result, before, after: video.currentTime, events,
              readyState: video.readyState, width: video.videoWidth, height: video.videoHeight,
              error: video.error?.code ?? null
            };
            video.pause();
            if (state.play !== 'resolved' || state.after - state.before < 0.25 || state.width < 1 || !events.includes('playing')) {
              throw new Error(`video-not-playing:${JSON.stringify(state)}`);
            }
            return state;
          };
          (async () => {
            try {
              await waitFor(() => document.querySelector('#job-status')?.textContent, 'app boot');
              document.querySelector('#library-settings').open = true;
              const engine = new URL(location.href).searchParams.get('engine') || 'color-motion';
              const selector = document.querySelector('#engine');
              selector.value = engine;
              selector.dispatchEvent(new Event('change', { bubbles: true }));
              document.querySelector('#choose-store').click();
              await waitFor(() => document.querySelector('#store-label').textContent === 'store', 'test store selected');
              document.querySelector('#add-video').click();
              await waitFor(() => document.querySelector('#library-count').textContent === '1本', 'synthetic clip selected');
              const query = document.querySelector('#query-player');
              const queryPlayback = await playback(query);

              document.querySelector('#start-index').click();
              await waitFor(() => ['complete','failed','cancelled'].includes(document.querySelector('#job-strip').dataset.status), 'index job');
              const indexStatus = document.querySelector('#job-strip').dataset.status;
              const indexMessage = document.querySelector('#job-status').textContent;
              if (indexStatus !== 'complete') throw new Error(`index-${indexStatus}:${indexMessage}`);
              const fileRow = document.querySelector('#library-list .file-row .file-state')?.textContent;
              const indexProgress = document.querySelector('#progress-caption').textContent;

              const searchAt = async start => {
                const input = document.querySelector('#query-start');
                input.value = String(start);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                await waitFor(() => document.querySelector('#start-search').getAttribute('aria-disabled') === 'false', 'search enabled');
                document.querySelector('#start-search').click();
                await waitFor(() => !document.querySelector('.match-card'), 'old results cleared');
                await waitFor(() => ['complete','failed','cancelled'].includes(document.querySelector('#job-strip').dataset.status), 'search job');
                const status = document.querySelector('#job-strip').dataset.status;
                if (status !== 'complete') throw new Error(`search-${status}:${document.querySelector('#job-status').textContent}`);
                await waitFor(() => document.querySelector('.match-card .button-compare'), 'candidate rendered');
                return document.querySelector('.match-card .match-time').textContent;
              };
              const candidateTime = await searchAt(0.5);
              document.querySelector('.match-card .button-compare').click();
              const candidate = document.querySelector('#candidate-player');
              const candidatePlayback = await playback(candidate);
              document.querySelector('.match-card .button-keep').click();
              await waitFor(() => document.querySelector('#reference-count').textContent === '1', 'keep decision saved');

              const heldTime = await searchAt(1.0);
              document.querySelector('.match-card .button-hold').click();
              await waitFor(() => document.querySelector('#reference-count').textContent === '2', 'hold decision saved');
              const referenceText = document.querySelector('#references-list').innerText;
              const fingerprint = document.querySelector('#result-provenance').textContent;
              const exportLink = document.querySelector('#export-references');
              const exportResponse = await fetch(exportLink.href, { credentials: 'same-origin' });
              const exportedRows = await exportResponse.json();
              window.__workflowResult = JSON.stringify({
                engine, selectedFiles: 1, fileRow, indexStatus, indexMessage, indexProgress,
                queryPlayback, candidateTime, candidatePlayback, heldTime,
                references: document.querySelector('#reference-count').textContent,
                referenceText, fingerprint, exportDisabled: exportLink.getAttribute('aria-disabled'),
                exportFetchStatus: exportResponse.status, exportRows: exportedRows.length,
                exportDecisions: exportedRows.map(row => row.decision).sort()
              });
              window.webkit.messageHandlers.qa.postMessage(window.__workflowResult);
              exportLink.click();
            } catch (error) {
              window.__workflowResult = JSON.stringify({ workflowError: `${error.name}:${error.message}` });
            }
          })();
          return 'workflow-started';
        })()
        """#
        webView.evaluateJavaScript(js) { _, error in
            if let error = error {
                print("WORKFLOW_JS_ERROR \(error.localizedDescription)")
                self.failed = true
                self.finished = true
            } else {
                self.pollPageResult()
            }
        }
    }

    private func pollPageResult(_ attempts: Int = 700) {
        webView.evaluateJavaScript("window.__workflowResult || null") { value, error in
            if let error = error {
                print("WORKFLOW_PAGE_ERROR \(error.localizedDescription)")
                self.failed = true
                self.finished = true
            } else if let raw = value as? String, let data = raw.data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                self.pageResult = object
                if object["workflowError"] != nil {
                    if !self.didPrintPageResult { print("WORKFLOW_RESULT \(raw)"); self.didPrintPageResult = true }
                    self.failed = true
                    self.finished = true
                } else {
                    if !self.didPrintPageResult { print("WORKFLOW_RESULT \(raw)"); self.didPrintPageResult = true }
                    self.pollDownload()
                }
            } else if attempts > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.pollPageResult(attempts - 1) }
            } else {
                print("WORKFLOW_TIMEOUT")
                self.failed = true
                self.finished = true
            }
        }
    }

    func recordWorkflowResult(_ body: Any) {
        guard let raw = body as? String, let data = raw.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            print("WORKFLOW_RESULT_INVALID \(body)")
            failed = true
            return
        }
        pageResult = object
        didPrintPageResult = true
        print("WORKFLOW_RESULT \(raw)")
        if object["workflowError"] != nil { failed = true }
    }

    private func pollDownload(_ attempts: Int = 50) {
        if FileManager.default.fileExists(atPath: downloadPath.path) {
            verifyDownloadedReferences()
        } else if attempts > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.pollDownload(attempts - 1) }
        } else {
            print("EXPORT_DOWNLOAD_TIMEOUT")
            failed = true
            finished = true
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if navigationResponse.response.url?.path == "/api/references/export" {
            let response = navigationResponse.response as? HTTPURLResponse
            print("EXPORT_RESPONSE status=\(response?.statusCode ?? 0) disposition=\(response?.value(forHTTPHeaderField: "Content-Disposition") ?? "")")
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        completionHandler(downloadPath)
    }

    func downloadDidFinish(_ download: WKDownload) {
        verifyDownloadedReferences()
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        print("EXPORT_DOWNLOAD_ERROR \(error.localizedDescription)")
        failed = true
        finished = true
    }

    private func verifyDownloadedReferences() {
        if exportVerified { return }
        guard let data = try? Data(contentsOf: downloadPath),
              let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
        let decisions = rows.compactMap { $0["decision"] as? String }
        print("EXPORT_DOWNLOAD rows=\(rows.count) decisions=\(decisions.sorted().joined(separator: ",")) bytes=\(data.count)")
        if rows.count != 2 || Set(decisions) != Set(["keep", "hold"]) { failed = true }
        exportVerified = true
        finished = true
    }
}

guard CommandLine.arguments.count == 3,
      let url = URL(string: CommandLine.arguments[1]), url.scheme == "http",
      url.host == "127.0.0.1", url.port != nil else {
    fputs("usage: qa_synthetic_ui_workflow.swift http://127.0.0.1:PORT/?engine=... OUTPUT.json\n", stderr)
    exit(2)
}
let downloadPath = URL(fileURLWithPath: CommandLine.arguments[2])
try? FileManager.default.removeItem(at: downloadPath)
NSApplication.shared.setActivationPolicy(.accessory)
let config = WKWebViewConfiguration()
config.websiteDataStore = .nonPersistent()
config.mediaTypesRequiringUserActionForPlayback = []
let bridge = WorkflowResultBridge()
config.userContentController.add(bridge, name: "qa")
let view = WKWebView(frame: CGRect(x: 0, y: 0, width: 1440, height: 1000), configuration: config)
let window = NSWindow(contentRect: view.frame, styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
window.title = "Synthetic JEPA UI Workflow QA"
window.contentView = view
window.center()
window.makeKeyAndOrderFront(nil)
NSApplication.shared.activate(ignoringOtherApps: true)
let qa = WorkflowQA(webView: view, downloadPath: downloadPath)
bridge.onResult = { qa.recordWorkflowResult($0) }
view.navigationDelegate = qa
view.load(URLRequest(url: url))
let deadline = Date().addingTimeInterval(220)
while !qa.finished && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.1)) }
if !qa.finished { print("WORKFLOW_TIMEOUT"); qa.failed = true }
try? FileManager.default.removeItem(at: downloadPath)
exit(qa.failed ? 1 : 0)
