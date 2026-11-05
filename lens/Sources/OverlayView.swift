import AppKit
import SwiftUI

/// In-place floating overlay attached to the captured screen region.
/// Number badges sit on the left edge of each paragraph; hovering a badge (or toggling "全部展开")
/// reveals a translucent translation card right below that paragraph without covering the rest.
@MainActor
final class InPlaceOverlayController {
    private var window: NSPanel?
    private var keyMonitor: Any?

    var isVisible: Bool { window?.isVisible == true }

    func toggle(for session: LensSession, onOpenWindow: @escaping () -> Void) {
        if isVisible {
            close()
        } else {
            show(for: session, onOpenWindow: onOpenWindow)
        }
    }

    func show(for session: LensSession, onOpenWindow: @escaping () -> Void) {
        close()
        guard let rect = session.screenRect else { return }
        let pad: CGFloat = 44
        let extraBottom: CGFloat = 180
        let minPanelWidth: CGFloat = 460
        let extraRight = max(0, minPanelWidth - (rect.width + pad * 2))
        let frame = CGRect(
            x: rect.minX - pad,
            y: rect.minY - pad - extraBottom,
            width: rect.width + pad * 2 + extraRight,
            height: rect.height + pad * 2 + extraBottom
        )
        let panel = NSPanel(
            contentRect: frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let root = InPlaceOverlayRootView(
            session: session,
            padding: pad,
            onOpenWindow: { [weak self] in
                self?.close()
                onOpenWindow()
            },
            onClose: { [weak self] in self?.close() }
        )
        panel.contentView = NSHostingView(rootView: root)
        panel.orderFrontRegardless()
        window = panel
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 {
                self?.close()
                return nil
            }
            return event
        }
    }

    func close() {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
        window?.orderOut(nil)
        window = nil
    }
}

struct InPlaceOverlayRootView: View {
    @ObservedObject var session: LensSession
    let padding: CGFloat
    let onOpenWindow: () -> Void
    let onClose: () -> Void
    @State private var hoveredID: Int?
    @State private var pinnedIDs: Set<Int> = []
    @State private var showAll = true
    @AppStorage("showOverlayToolbar") private var showToolbar = false

    var body: some View {
        let size = session.pointSize
        let items = session.marginItems
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(lensAccent.opacity(0.32), style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .frame(width: size.width, height: size.height)
                .offset(x: padding, y: padding)
                .allowsHitTesting(false)

            if showToolbar {
                HStack(spacing: 10) {
                    Text("鹊桥悬浮")
                        .font(.system(size: 11, weight: .semibold))
                    if !session.finished {
                        ProgressView().controlSize(.mini).tint(.white)
                    }
                    Button(showAll ? "仅悬停显示" : "全部展开") {
                        showAll.toggle()
                    }
                    .buttonStyle(.plain)
                    .font(.system(size: 11))
                    if session.onRecapture != nil {
                        Button("重截 \(LensSettings.modifierSymbol)\(LensSettings.recaptureKey)") { session.onRecapture?() }
                            .buttonStyle(.plain)
                            .font(.system(size: 11))
                    }
                    Button("对照窗口") { onOpenWindow() }
                        .buttonStyle(.plain)
                        .font(.system(size: 11))
                    Button("隐藏工具栏") { showToolbar = false }
                        .buttonStyle(.plain)
                        .font(.system(size: 11))
                    Button("×") { onClose() }
                        .buttonStyle(.plain)
                        .font(.system(size: 13, weight: .bold))
                }
                .fixedSize()
                .padding(.horizontal, 12)
                .padding(.vertical, 5)
                .foregroundStyle(.white)
                .background(Capsule().fill(lensAccent.opacity(0.92)))
                .shadow(color: .black.opacity(0.2), radius: 4, y: 1)
                .offset(x: padding, y: max(6, padding - 32))
            } else {
                HStack(spacing: 4) {
                    if !session.finished {
                        ProgressView().controlSize(.mini)
                    }
                    Menu {
                        Button(showAll ? "切换为仅悬停显示" : "全部展开译文卡片") { showAll.toggle() }
                        if session.onRecapture != nil {
                            Button("重新截取（\(LensSettings.modifierSymbol)\(LensSettings.recaptureKey)）") { session.onRecapture?() }
                        }
                        Button("在独立对照窗口打开") { onOpenWindow() }
                        Divider()
                        Button("显示顶部工具栏") { showToolbar = true }
                        Button("关闭悬浮层") { onClose() }
                    } label: {
                        Image(systemName: "ellipsis")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.secondary)
                            .frame(width: 18, height: 18)
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .fixedSize()

                    Button {
                        onClose()
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 9.5, weight: .bold))
                            .foregroundStyle(.secondary)
                            .frame(width: 18, height: 18)
                    }
                    .buttonStyle(.plain)
                    .help("关闭悬浮译文")
                }
                .padding(.horizontal, 5)
                .padding(.vertical, 2)
                .background(Capsule().fill(.regularMaterial))
                .overlay(Capsule().strokeBorder(Color.secondary.opacity(0.22), lineWidth: 0.8))
                .offset(x: padding + max(size.width - 44, 0), y: max(8, padding - 24))
            }

            ForEach(Array(items.enumerated()), id: \.element.id) { index, paragraph in
                let rect = session.pointRect(paragraph)
                let active = showAll || hoveredID == paragraph.id || pinnedIDs.contains(paragraph.id)

                if items.count > 1 || !showAll {
                    Button {
                        if pinnedIDs.contains(paragraph.id) {
                            pinnedIDs.remove(paragraph.id)
                        } else {
                            pinnedIDs.insert(paragraph.id)
                        }
                    } label: {
                        Text("\(index + 1)")
                            .font(.system(size: 10, weight: .bold, design: .rounded))
                            .foregroundStyle(.white)
                            .frame(width: 18, height: 18)
                            .background(Circle().fill(active ? lensAccent : lensAccent.opacity(0.82)))
                            .shadow(color: .black.opacity(0.22), radius: 2, y: 1)
                    }
                    .buttonStyle(.plain)
                    .onHover { inside in
                        if inside { hoveredID = paragraph.id } else if hoveredID == paragraph.id { hoveredID = nil }
                    }
                    .offset(x: max(4, padding + rect.minX - 22), y: padding + rect.minY)
                }

                if active {
                    HStack(alignment: .top, spacing: 8) {
                        TranslationText(paragraph: paragraph, size: 13)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .frame(minWidth: min(rect.width, 160), maxWidth: max(size.width, 360), alignment: .leading)
                    .fixedSize(horizontal: rect.width < 220, vertical: true)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .fill(.regularMaterial)
                            .overlay(
                                RoundedRectangle(cornerRadius: 8)
                                    .strokeBorder(lensAccent.opacity(0.45), lineWidth: 1)
                            )
                    )
                    .shadow(color: .black.opacity(0.18), radius: 8, y: 3)
                    .contextMenu {
                        Button("复制这段译文") {
                            if let t = paragraph.translation {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(t, forType: .string)
                            }
                        }
                        Button("在独立对照窗口打开") { onOpenWindow() }
                        Divider()
                        Button("关闭悬浮译文") { onClose() }
                    }
                    .onHover { inside in
                        if inside { hoveredID = paragraph.id } else if hoveredID == paragraph.id { hoveredID = nil }
                    }
                    .offset(x: padding + rect.minX, y: padding + rect.maxY + 4)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
