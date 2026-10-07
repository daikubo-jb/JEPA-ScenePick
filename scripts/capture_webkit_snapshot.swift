import AppKit
import Foundation
import ImageIO
import UniformTypeIdentifiers
import WebKit

final class SnapshotDelegate: NSObject, WKNavigationDelegate {
    let webView: WKWebView
    let output: URL
    var finished = false
    var failure: String?

    init(webView: WKWebView, output: URL) {
        self.webView = webView
        self.output = output
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            webView.evaluateJavaScript("(()=>{const b=document.querySelector('#start-search'),root=getComputedStyle(document.documentElement),saved=b.getAttribute('aria-disabled'),current=getComputedStyle(b).backgroundColor;b.setAttribute('aria-disabled','true');const disabledBG=getComputedStyle(b).backgroundColor,disabledPaint=getComputedStyle(b,'::before').backgroundColor;b.setAttribute('aria-disabled','false');const enabledBG=getComputedStyle(b).backgroundColor,enabledPaint=getComputedStyle(b,'::before').backgroundColor;b.setAttribute('aria-disabled',saved);const restoredBG=getComputedStyle(b).backgroundColor,restoredPaint=getComputedStyle(b,'::before').backgroundColor;const rules=Array.from(document.styleSheets).flatMap(x=>{try{return Array.from(x.cssRules)}catch{return []}});return JSON.stringify({files:document.querySelector('#library-count').textContent,engine:document.querySelector('#engine-summary').textContent,query:document.querySelector('#query-file').value,range:document.querySelector('#active-range-label').textContent,provenance:document.querySelector('#result-provenance').textContent,searchDisabled:saved==='true',nativeDisabled:b.disabled,disabledPseudo:b.matches(':disabled'),searchBackground:current,disabledBackground:disabledBG,enabledBackground:enabledBG,restoredBackground:restoredBG,disabledPaint,enabledPaint,restoredPaint,blueToken:root.getPropertyValue('--blue').trim(),searchColor:getComputedStyle(b).color,searchAppearance:getComputedStyle(b).appearance,searchClass:b.className,searchInline:b.getAttribute('style'),matchingRules:rules.filter(r=>r.type===1&&b.matches(r.selectorText)).map(r=>r.cssText),candidateTabDisabled:document.querySelector('#candidate-view-tab').disabled,status:document.querySelector('#job-status').textContent,workbench:document.querySelector('.workbench').getBoundingClientRect().height,scrollWidth:document.documentElement.scrollWidth,clientWidth:document.documentElement.clientWidth})})()") { value, _ in
                if let value = value { print("WebKit UI state: \(value)") }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    self.captureThenExercise(webView)
                }
            }
        }
    }

    private func captureThenExercise(_ webView: WKWebView) {
        let config = WKSnapshotConfiguration()
        config.rect = CGRect(x: 0, y: 0, width: 1360, height: 1000)
        webView.takeSnapshot(with: config) { image, error in
                if let error = error {
                    self.failure = error.localizedDescription
                    self.finished = true
                    return
                }
                guard let image = image, let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil),
                      let destination = CGImageDestinationCreateWithURL(self.output as CFURL, UTType.png.identifier as CFString, 1, nil) else {
                    self.failure = "WebKit did not provide a PNG image"
                    self.finished = true
                    return
                }
                CGImageDestinationAddImage(destination, cgImage, nil)
                if !CGImageDestinationFinalize(destination) {
                    self.failure = "WebKit could not finalize the PNG image"
                    self.finished = true
                    return
                }
                let qa = "(()=>{const qctx=document.querySelector('#query-context'),cctx=document.querySelector('#candidate-context');qctx.click();const queryContext=document.querySelector('#active-range-label').textContent;qctx.click();document.querySelector('.view-tabs').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));const arrowRight=document.querySelector('#candidate-view-tab').getAttribute('aria-selected');document.querySelector('.view-tabs').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));const arrowLeft=document.querySelector('#query-view-tab').getAttribute('aria-selected');document.querySelector('.match-card .button-compare')?.click();const compare=document.querySelector('#candidate-view-tab').getAttribute('aria-selected');cctx.click();const candidateContext=document.querySelector('#active-range-label').textContent;cctx.click();document.querySelector('.match-card .button-keep')?.click();document.querySelector('.match-card .button-keep')?.click();setTimeout(()=>{const refs=document.querySelector('#reference-count').textContent;document.querySelector('#engine').value='vjepa2';document.querySelector('#engine').dispatchEvent(new Event('change',{bubbles:true}));const mismatch=document.querySelector('#result-engine-mismatch').hidden===false;document.querySelector('#engine').value='color-motion';document.querySelector('#engine').dispatchEvent(new Event('change',{bubbles:true}));const oldFetch=window.fetch;let searchRequests=0;window.fetch=(...args)=>{if(String(args[0])==='/api/search')searchRequests++;return oldFetch(...args)};const search=document.querySelector('#start-search');search.click();search.click();const disabledDuringSubmit=search.getAttribute('aria-disabled')==='true';setTimeout(()=>{window.fetch=oldFetch;window.__qaResult=JSON.stringify({queryContext,arrowRight,arrowLeft,compare,candidateContext,referenceCount:refs,mismatch,searchRequests,disabledDuringSubmit})},600)},600);return 'interaction sequence started'})()"
                webView.evaluateJavaScript(qa) { value, error in
                    if let error = error {
                        print("WebKit interaction QA failed: \(error.localizedDescription)")
                        self.failure = error.localizedDescription
                        self.finished = true
                    } else {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                            webView.evaluateJavaScript("window.__qaResult || 'interaction result missing'") { result, resultError in
                                if let resultError = resultError {
                                    print("WebKit interaction QA failed: \(resultError.localizedDescription)")
                                    self.failure = resultError.localizedDescription
                                } else if let result = result {
                                    print("WebKit synthetic interaction QA: \(result)")
                                }
                                self.finished = true
                            }
                        }
                    }
                }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failure = error.localizedDescription
        finished = true
    }
}

guard CommandLine.arguments.count == 3,
      let address = URL(string: CommandLine.arguments[1]),
      address.scheme == "http", address.host == "127.0.0.1",
      address.path == "/", address.port != nil else {
    fputs("usage: capture_webkit_snapshot.swift http://127.0.0.1:PORT/ OUTPUT.png\n", stderr)
    exit(2)
}
let output = URL(fileURLWithPath: CommandLine.arguments[2])
NSApplication.shared.setActivationPolicy(.accessory)
let configuration = WKWebViewConfiguration()
configuration.websiteDataStore = .nonPersistent()
let webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 1360, height: 1000), configuration: configuration)
let window = NSWindow(contentRect: webView.frame, styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
window.title = "Synthetic UI Review"
window.contentView = webView
window.center()
window.makeKeyAndOrderFront(nil)
NSApplication.shared.activate(ignoringOtherApps: true)
let delegate = SnapshotDelegate(webView: webView, output: output)
webView.navigationDelegate = delegate
webView.load(URLRequest(url: address))
let deadline = Date().addingTimeInterval(20)
while !delegate.finished && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}
if !delegate.finished {
    fputs("WebKit snapshot timed out\n", stderr)
    exit(1)
}
if let failure = delegate.failure {
    fputs("WebKit snapshot failed: \(failure)\n", stderr)
    exit(1)
}
print("Captured WebKit UI screenshot: \(output.lastPathComponent)")
