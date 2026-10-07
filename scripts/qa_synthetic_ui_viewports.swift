import AppKit
import Foundation
import WebKit

final class ViewportQA: NSObject, WKNavigationDelegate {
    let webView: WKWebView
    var finished = false
    var failed: String?

    init(webView: WKWebView) { self.webView = webView }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.probe(width: 1024, zoom: 1, label: "1024px") {
                self.probe(width: 1024, zoom: 2, label: "1024px at 200%") {
                    self.finished = true
                }
            }
        }
    }

    private func probe(width: CGFloat, zoom: CGFloat, label: String, done: @escaping () -> Void) {
        webView.pageZoom = 1
        webView.window?.setContentSize(NSSize(width: width, height: 1000))
        webView.setFrameSize(NSSize(width: width, height: 1000))
        webView.layoutSubtreeIfNeeded()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            self.webView.pageZoom = zoom
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                let js = "(()=>{const d=document.documentElement,b=document.querySelector('#start-search'),r=b.getBoundingClientRect(),tabs=document.querySelector('.view-tabs'),panel=document.querySelector('.workbench');b.focus();const focusable=document.activeElement===b,focusOutline=getComputedStyle(b).outlineStyle; b.blur();return JSON.stringify({label:'\(label)',viewport:innerWidth,clientWidth:d.clientWidth,scrollWidth:d.scrollWidth,overflow:d.scrollWidth>d.clientWidth,workbenchWidth:Math.round(panel.getBoundingClientRect().width),searchEnabled:b.getAttribute('aria-disabled')!=='true',searchVisible:r.width>0&&r.height>0,searchHeight:Math.round(r.height),searchPaint:getComputedStyle(b,'::before').backgroundColor,searchBackground:getComputedStyle(b).backgroundColor,keepBackground:getComputedStyle(document.querySelector('.button-keep')).backgroundColor,tabsVisible:tabs.getBoundingClientRect().width>0,resultsVisible:document.querySelector('.results-panel').getBoundingClientRect().width>0,focusable,focusOutline})})()"
                self.webView.evaluateJavaScript(js) { value, error in
                    if let error {
                        self.failed = error.localizedDescription
                        fputs("Viewport QA failed (\(label)): \(error.localizedDescription)\n", stderr)
                    } else {
                        print("WebKit viewport QA: \(value ?? "no result")")
                    }
                    done()
                }
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failed = error.localizedDescription
        finished = true
    }
}

guard CommandLine.arguments.count == 2,
      let address = URL(string: CommandLine.arguments[1]),
      address.scheme == "http", address.host == "127.0.0.1", address.port != nil else {
    fputs("usage: qa_synthetic_ui_viewports.swift http://127.0.0.1:PORT/\n", stderr)
    exit(2)
}

NSApplication.shared.setActivationPolicy(.accessory)
let config = WKWebViewConfiguration()
config.websiteDataStore = .nonPersistent()
let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1360, height: 1000), configuration: config)
let window = NSWindow(contentRect: webView.frame, styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
window.contentView = webView
window.makeKeyAndOrderFront(nil)
let qa = ViewportQA(webView: webView)
webView.navigationDelegate = qa
webView.load(URLRequest(url: address))
let deadline = Date().addingTimeInterval(20)
while !qa.finished && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}
if !qa.finished {
    fputs("Viewport QA timed out\n", stderr)
    exit(1)
}
if let failed = qa.failed {
    fputs("Viewport QA failed: \(failed)\n", stderr)
    exit(1)
}
