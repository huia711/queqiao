import AppKit
import SwiftUI

/// In-place floating overlay attached to the captured screen region.
/// Number badges sit on the left edge of each paragraph; hovering a badge (or toggling "全部展开")
/// reveals a translucent translation card right below that paragraph without covering the rest.
@MainActor
final class InPlaceOverlayController {
    private var window: NSPanel?

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
        let frame = CGRect(
            x: rect.minX - pad,
            y: rect.minY - pad - extraBottom,
            width: rect.width + pad * 2,
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
    }

    func close() {
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

    var body: some View {
        let size = session.pointSize
        let items = session.marginItems
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(lensAccent.opacity(0.45), style: StrokeStyle(lineWidth: 1.2, dash: [5, 4]))
                .frame(width: size.width, height: size.height)
                .offset(x: padding, y: padding)
                .allowsHitTesting(false)

            HStack(spacing: 10) {
                Text("鹊桥原位悬浮")
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
                Button("×") { onClose() }
                    .buttonStyle(.plain)
                    .font(.system(size: 13, weight: .bold))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
            .foregroundStyle(.white)
            .background(Capsule().fill(lensAccent.opacity(0.92)))
            .shadow(color: .black.opacity(0.2), radius: 4, y: 1)
            .offset(x: padding, y: max(6, padding - 32))

            ForEach(Array(items.enumerated()), id: \.element.id) { index, paragraph in
                let rect = session.pointRect(paragraph)
                let active = showAll || hoveredID == paragraph.id || pinnedIDs.contains(paragraph.id)

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
                        .frame(width: 20, height: 20)
                        .background(Circle().fill(active ? lensAccent : lensAccent.opacity(0.82)))
                        .shadow(color: .black.opacity(0.25), radius: 2, y: 1)
                }
                .buttonStyle(.plain)
                .onHover { inside in
                    if inside { hoveredID = paragraph.id } else if hoveredID == paragraph.id { hoveredID = nil }
                }
                .offset(x: max(4, padding + rect.minX - 24), y: padding + rect.minY)

                if active {
                    VStack(alignment: .leading, spacing: 4) {
                        TranslationText(paragraph: paragraph, size: 13)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .frame(width: min(max(rect.width, 240), size.width), alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .fill(.regularMaterial)
                            .overlay(
                                RoundedRectangle(cornerRadius: 8)
                                    .strokeBorder(lensAccent.opacity(0.45), lineWidth: 1)
                            )
                    )
                    .shadow(color: .black.opacity(0.18), radius: 8, y: 3)
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
