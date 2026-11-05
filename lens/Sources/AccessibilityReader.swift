import AppKit
import ApplicationServices
import Carbon.HIToolbox

enum AccessibilityReader {
    static func isTrusted(prompt: Bool = false) -> Bool {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options = [key: prompt] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    /// Reads the currently selected text from the frontmost app via Accessibility, falling back to
    /// a simulated ⌘C that preserves and restores the user's clipboard.
    @MainActor
    static func readSelectedText() async -> String? {
        if isTrusted(prompt: false), let axText = axSelectedText(), !axText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return axText
        }
        return await copyViaSimulatedShortcut()
    }

    private static func axSelectedText() -> String? {
        let systemWide = AXUIElementCreateSystemWide()
        var focusedObj: CFTypeRef?
        guard AXUIElementCopyAttributeValue(systemWide, kAXFocusedUIElementAttribute as CFString, &focusedObj) == .success,
              let focused = focusedObj
        else { return nil }
        let element = focused as! AXUIElement
        var selectedObj: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXSelectedTextAttribute as CFString, &selectedObj) == .success,
           let str = selectedObj as? String, !str.isEmpty {
            return str
        }
        return nil
    }

    @MainActor
    private static func copyViaSimulatedShortcut() async -> String? {
        let pb = NSPasteboard.general
        let oldChangeCount = pb.changeCount
        let savedString = pb.string(forType: .string)

        guard let src = CGEventSource(stateID: .hidSystemState),
              let down = CGEvent(keyboardEventSource: src, virtualKey: CGKeyCode(kVK_ANSI_C), keyDown: true),
              let up = CGEvent(keyboardEventSource: src, virtualKey: CGKeyCode(kVK_ANSI_C), keyDown: false)
        else { return nil }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)

        try? await Task.sleep(nanoseconds: 140_000_000)
        guard pb.changeCount != oldChangeCount,
              let copied = pb.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !copied.isEmpty
        else { return nil }

        pb.clearContents()
        if let savedString { pb.setString(savedString, forType: .string) }
        return copied
    }

    /// Samples Accessibility text elements inside `rect` (AppKit global screen coordinates) so native
    /// apps like Safari, Preview, and Notes can supply exact strings to fix OCR typos.
    static func sampleStrings(in rect: CGRect) -> [String] {
        guard isTrusted(prompt: false),
              let primaryHeight = NSScreen.screens.first?.frame.height
        else { return [] }
        let systemWide = AXUIElementCreateSystemWide()
        var collected: [String] = []
        var seen: Set<String> = []
        let cols = 4
        let rows = 6
        for r in 0..<rows {
            for c in 0..<cols {
                let appKitX = rect.minX + rect.width * (CGFloat(c) + 0.5) / CGFloat(cols)
                let appKitY = rect.minY + rect.height * (CGFloat(r) + 0.5) / CGFloat(rows)
                let cgY = primaryHeight - appKitY
                var element: AXUIElement?
                guard AXUIElementCopyElementAtPosition(systemWide, Float(appKitX), Float(cgY), &element) == .success,
                      let el = element
                else { continue }
                for candidate in extractStrings(from: el) {
                    let cleaned = candidate.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard cleaned.count >= 8, !seen.contains(cleaned) else { continue }
                    seen.insert(cleaned)
                    collected.append(cleaned)
                }
            }
        }
        return collected
    }

    private static func extractStrings(from element: AXUIElement) -> [String] {
        var out: [String] = []
        for attr in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute] {
            var value: CFTypeRef?
            if AXUIElementCopyAttributeValue(element, attr as CFString, &value) == .success,
               let str = value as? String, !str.isEmpty {
                for block in str.components(separatedBy: .newlines) {
                    let t = block.trimmingCharacters(in: .whitespacesAndNewlines)
                    if t.count >= 8 { out.append(t) }
                }
            }
        }
        return out
    }
}
