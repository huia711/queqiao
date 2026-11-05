import AppKit
import SwiftUI

/// Where translation cards go relative to the captured region.
enum OverlaySide {
    case right
    case left
    case below
}

struct OverlayGeometry {
    /// Captured region inside the panel, in points, top-left origin.
    var region: CGRect
    /// Side column for translation cards (empty for `.below`).
    var column: CGRect
    var side: OverlaySide
}

/// In-place translation attached to the captured screen region.
/// When there is room beside the region, translations sit in a column next to it, each aligned with its
/// paragraph, so the original text is never covered. Otherwise cards appear under a paragraph on hover.
@MainActor
final class InPlaceOverlayController {
    private var window: NSPanel?
    private var monitors: [Any] = []

    var isVisible: Bool { window?.isVisible == true }

    func show(for session: LensSession, onOpenWindow: @escaping () -> Void) {
        close()
        guard let rect = session.screenRect else { return }
        let screen = NSScreen.screens.first { $0.frame.intersects(rect) } ?? NSScreen.main!
        let visible = screen.visibleFrame
        let margin: CGFloat = 26
        let gap: CGFloat = 12
        let rightSpace = visible.maxX - rect.maxX - gap
        let leftSpace = rect.minX - visible.minX - gap
        let wanted: CGFloat = 380
        let minimum: CGFloat = 260

        var side: OverlaySide = .below
        var columnWidth: CGFloat = 0
        if rightSpace >= minimum {
            side = .right
            columnWidth = min(wanted, rightSpace)
        } else if leftSpace >= minimum {
            side = .left
            columnWidth = min(wanted, leftSpace)
        }

        let bottomRoom: CGFloat = side == .below ? 200 : 260
        var frame = rect.insetBy(dx: -margin, dy: -margin)
        frame.origin.y -= bottomRoom
        frame.size.height += bottomRoom
        if side == .right { frame.size.width += gap + columnWidth }
        if side == .left {
            frame.origin.x -= gap + columnWidth
            frame.size.width += gap + columnWidth
        }
        frame = frame.intersection(visible.insetBy(dx: -margin, dy: -margin))

        let region = CGRect(
            x: rect.minX - frame.minX,
            y: frame.maxY - rect.maxY,
            width: rect.width,
            height: rect.height
        )
        let column: CGRect
        switch side {
        case .right: column = CGRect(x: region.maxX + gap, y: region.minY, width: columnWidth, height: 0)
        case .left: column = CGRect(x: region.minX - gap - columnWidth, y: region.minY, width: columnWidth, height: 0)
        case .below: column = .zero
        }

        let panel = NSPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.acceptsMouseMovedEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        let root = InPlaceOverlayRootView(
            session: session,
            geometry: OverlayGeometry(region: region, column: column, side: side),
            onOpenWindow: { [weak self] in
                self?.close()
                onOpenWindow()
            },
            onClose: { [weak self] in self?.close() }
        )
        panel.contentView = NSHostingView(rootView: root)
        panel.orderFrontRegardless()
        window = panel

        let escape: (NSEvent) -> Bool = { $0.keyCode == 53 }
        if let global = NSEvent.addGlobalMonitorForEvents(matching: .keyDown, handler: { [weak self] event in
            if escape(event) { Task { @MainActor in self?.close() } }
        }) { monitors.append(global) }
        if let local = NSEvent.addLocalMonitorForEvents(matching: .keyDown, handler: { [weak self] event in
            if escape(event) {
                self?.close()
                return nil
            }
            return event
        }) { monitors.append(local) }
    }

    func close() {
        monitors.forEach(NSEvent.removeMonitor)
        monitors.removeAll()
        window?.orderOut(nil)
        window = nil
    }
}

struct InPlaceOverlayRootView: View {
    @ObservedObject var session: LensSession
    let geometry: OverlayGeometry
    let onOpenWindow: () -> Void
    let onClose: () -> Void
    @State private var hoveredID: Int?
    @State private var pinnedIDs: Set<Int> = []
    @State private var collapsed = false
    @AppStorage("showOverlayToolbar") private var showToolbar = false

    private var region: CGRect { geometry.region }
    private var usesColumn: Bool { geometry.side != .below && !collapsed }

    var body: some View {
        let items = session.marginItems
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(lensAccent.opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .frame(width: region.width + 8, height: region.height + 8)
                .offset(x: region.minX - 4, y: region.minY - 4)
                .allowsHitTesting(false)

            ForEach(Array(items.enumerated()), id: \.element.id) { index, paragraph in
                let rect = paragraphRect(paragraph)
                if hoveredID == paragraph.id {
                    RoundedRectangle(cornerRadius: 4)
                        .fill(lensAccent.opacity(0.10))
                        .frame(width: rect.width + 8, height: rect.height + 6)
                        .offset(x: rect.minX - 4, y: rect.minY - 3)
                        .allowsHitTesting(false)
                }
                if items.count > 1 || !usesColumn {
                    badge(index + 1, paragraph: paragraph)
                        .offset(x: max(2, rect.minX - 24), y: rect.minY)
                }
                if !usesColumn, hoveredID == paragraph.id || pinnedIDs.contains(paragraph.id) {
                    card(paragraph, number: nil)
                        .frame(width: max(rect.width, 280), alignment: .leading)
                        .offset(x: rect.minX, y: rect.maxY + 4)
                }
            }

            if usesColumn {
                MarginLayout(anchors: items.map { paragraphRect($0).minY - geometry.column.minY }, spacing: 6) {
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, paragraph in
                        card(paragraph, number: items.count > 1 ? index + 1 : nil)
                    }
                }
                .frame(width: geometry.column.width, alignment: .topLeading)
                .offset(x: geometry.column.minX, y: geometry.column.minY)
            }

            controls
                .offset(x: controlsX, y: max(2, region.minY - 26))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func paragraphRect(_ paragraph: Paragraph) -> CGRect {
        session.pointRect(paragraph).offsetBy(dx: region.minX, dy: region.minY)
    }

    private var controlsX: CGFloat {
        switch geometry.side {
        case .right where usesColumn: return geometry.column.maxX - 56
        case .left where usesColumn: return geometry.column.minX
        default: return max(region.minX, region.maxX - 56)
        }
    }

    private func badge(_ number: Int, paragraph: Paragraph) -> some View {
        let active = hoveredID == paragraph.id || pinnedIDs.contains(paragraph.id)
        return Button {
            if pinnedIDs.contains(paragraph.id) { pinnedIDs.remove(paragraph.id) } else { pinnedIDs.insert(paragraph.id) }
        } label: {
            Text("\(number)")
                .font(.system(size: 10, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
                .frame(width: 18, height: 18)
                .background(Circle().fill(active ? lensAccent : lensAccent.opacity(0.78)))
                .shadow(color: .black.opacity(0.22), radius: 2, y: 1)
        }
        .buttonStyle(.plain)
        .onHover { inside in hover(paragraph.id, inside) }
    }

    private func card(_ paragraph: Paragraph, number: Int?) -> some View {
        HStack(alignment: .top, spacing: 7) {
            if let number {
                Text("\(number)")
                    .font(.system(size: 9, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
                    .frame(width: 15, height: 15)
                    .background(Circle().fill(lensAccent))
                    .padding(.top, 2)
            }
            TranslationText(paragraph: paragraph, size: 13)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(.regularMaterial)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(lensAccent.opacity(hoveredID == paragraph.id ? 0.75 : 0.35), lineWidth: 1)
                )
        )
        .shadow(color: .black.opacity(0.14), radius: 6, y: 2)
        .onHover { inside in hover(paragraph.id, inside) }
        .contextMenu {
            Button("复制这段译文") {
                if let text = paragraph.translation {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                }
            }
            Button("在独立对照窗口打开") { onOpenWindow() }
            Divider()
            Button("关闭悬浮译文") { onClose() }
        }
    }

    private func hover(_ id: Int, _ inside: Bool) {
        if inside { hoveredID = id } else if hoveredID == id { hoveredID = nil }
    }

    private var controls: some View {
        HStack(spacing: 4) {
            if !session.finished { ProgressView().controlSize(.mini) }
            if showToolbar {
                if session.onRecapture != nil {
                    Button("重截") { session.onRecapture?() }.buttonStyle(.plain).font(.system(size: 11))
                }
                Button("对照窗口") { onOpenWindow() }.buttonStyle(.plain).font(.system(size: 11))
            }
            Menu {
                if geometry.side != .below {
                    Button(collapsed ? "在旁边显示全部译文" : "收起为编号（悬停查看）") { collapsed.toggle() }
                }
                if session.onRecapture != nil {
                    Button("重新截取（\(LensSettings.modifierSymbol)\(LensSettings.recaptureKey)）") { session.onRecapture?() }
                }
                Button("在独立对照窗口打开") { onOpenWindow() }
                Button("复制全部译文") { session.copy(bilingual: false) }
                Divider()
                Button(showToolbar ? "隐藏快捷按钮" : "显示快捷按钮") { showToolbar.toggle() }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 10, weight: .bold))
                    .frame(width: 18, height: 18)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 9.5, weight: .bold))
                    .frame(width: 18, height: 18)
            }
            .buttonStyle(.plain)
            .help("关闭（Esc）")
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 5)
        .padding(.vertical, 2)
        .background(Capsule().fill(.regularMaterial))
        .overlay(Capsule().strokeBorder(Color.secondary.opacity(0.22), lineWidth: 0.8))
        .fixedSize()
    }
}
