import AppKit
import SwiftUI

enum ViewMode: String, CaseIterable, Identifiable {
    case bilingual = "对照"
    case margin = "旁注"
    var id: String { rawValue }
}

struct ResultView: View {
    @ObservedObject var session: LensSession
    @State var mode: ViewMode = .bilingual
    @State private var copied: String?
    @AppStorage("mergeDiagramLabels") private var mergeDiagramLabels = true
    @AppStorage("skipShortLabels") private var skipShortLabels = false
    @AppStorage("alwaysOnTop") private var alwaysOnTop = true
    var onAlwaysOnTopChanged: ((Bool) -> Void)? = nil

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            if let error = session.error, session.paragraphs.isEmpty {
                message(error, systemImage: "exclamationmark.triangle")
            } else if session.paragraphs.isEmpty {
                message(session.status, systemImage: nil)
            } else {
                if session.image == nil {
                    BilingualView(session: session)
                } else {
                    switch mode {
                    case .bilingual: BilingualView(session: session)
                    case .margin: MarginView(session: session)
                    }
                }
            }
        }
        .frame(minWidth: 480, minHeight: 320)
        .background(Color(nsColor: .textBackgroundColor))
    }

    private var toolbar: some View {
        HStack(spacing: 10) {
            if session.image != nil {
                Picker("", selection: $mode) {
                    ForEach(ViewMode.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 120)
            }
            if !session.finished { ProgressView().controlSize(.small) }
            Text(session.summary)
                .font(.system(size: 11.5))
                .foregroundStyle(session.error == nil ? Color.secondary : Color.red)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer()
            if let copied {
                Text(copied).font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            if session.onRecapture != nil {
                Button {
                    session.onRecapture?()
                } label: {
                    Label("重截", systemImage: "arrow.clockwise")
                        .font(.system(size: 11.5))
                }
                .help("重新截取同一区域（\(LensSettings.modifierSymbol)\(LensSettings.recaptureKey)）")
            }
            if session.screenRect != nil, session.onToggleOverlay != nil {
                Button {
                    session.onToggleOverlay?()
                } label: {
                    Label("原位悬浮", systemImage: "rectangle.on.rectangle")
                        .font(.system(size: 11.5))
                }
                .help("在原屏幕位置旁贴上悬浮译文角标")
            }
            if session.image != nil {
                Menu("选项") {
                    Toggle("合并图内短标签", isOn: $mergeDiagramLabels)
                        .onChange(of: mergeDiagramLabels) { _, _ in
                            Task { await session.rebuildLayoutAndRetranslate() }
                        }
                    Toggle("跳过图内短标签", isOn: $skipShortLabels)
                        .onChange(of: skipShortLabels) { _, _ in
                            Task { await session.rebuildLayoutAndRetranslate() }
                        }
                    Divider()
                    Toggle("窗口始终置顶", isOn: $alwaysOnTop)
                        .onChange(of: alwaysOnTop) { _, value in
                            onAlwaysOnTopChanged?(value)
                        }
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
            Menu("复制/导出") {
                Button("复制译文") { copy(false) }
                Button("复制原文 + 译文") { copy(true) }
                Button("复制 Markdown") { copyMarkdown() }
                Divider()
                Button("导出 Markdown 文件…") { session.exportMarkdownFile() }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .disabled(session.paragraphs.isEmpty)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
    }

    private func copy(_ bilingual: Bool) {
        session.copy(bilingual: bilingual)
        copied = "已复制"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { copied = nil }
    }

    private func copyMarkdown() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(session.markdown(), forType: .string)
        copied = "已复制 MD"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { copied = nil }
    }

    private func message(_ text: String, systemImage: String?) -> some View {
        VStack(spacing: 10) {
            if let systemImage {
                Image(systemName: systemImage).font(.system(size: 26)).foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
            Text(text).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .padding(30)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct TranslationText: View {
    let paragraph: Paragraph
    var size: CGFloat = 14

    var body: some View {
        if let text = paragraph.translation {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(paragraph.streaming ? text + " ▍" : text)
                    .font(.system(size: size))
                    .foregroundStyle(paragraph.failed ? Color.red : Color.primary)
                    .lineSpacing(3)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                if paragraph.cached {
                    Text("缓存")
                        .font(.system(size: 9.5))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(Color.secondary.opacity(0.12)))
                }
            }
        } else {
            HStack(spacing: 6) {
                ProgressView().controlSize(.mini)
                Text("翻译中…").font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
    }
}

/// Original paragraphs as screenshots, each followed by its translation.
struct BilingualView: View {
    @ObservedObject var session: LensSession

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                ForEach(session.paragraphs) { paragraph in
                    VStack(alignment: .leading, spacing: 7) {
                        if let crop = session.crop(for: paragraph) {
                            Image(nsImage: crop)
                                .interpolation(.high)
                                .resizable()
                                .aspectRatio(contentMode: .fit)
                                .frame(maxWidth: min(crop.size.width, 820), alignment: .leading)
                        } else {
                            Text(paragraph.text)
                                .font(.system(size: 13))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                        if !paragraph.skip {
                            TranslationText(paragraph: paragraph)
                                .padding(.leading, 10)
                                .overlay(alignment: .leading) {
                                    Rectangle().fill(lensAccent.opacity(0.55)).frame(width: 2.5)
                                }
                        } else if let reason = paragraph.skipReason, reason == "公式/代码" {
                            Text("保留\(reason)原样")
                                .font(.system(size: 11))
                                .foregroundStyle(.tertiary)
                        }
                    }
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// The original capture on the left with numbered paragraphs; translations aligned on the right.
struct MarginView: View {
    @ObservedObject var session: LensSession
    @State private var hovered: Int?

    var body: some View {
        GeometryReader { proxy in
            let size = session.pointSize
            let leftWidth = max(260, min(size.width, proxy.size.width * 0.56))
            let scale = leftWidth / size.width
            let height = size.height * scale
            let items = session.marginItems
            ScrollView {
                HStack(alignment: .top, spacing: 18) {
                    ZStack(alignment: .topLeading) {
                        if let cgImage = session.image {
                            Image(nsImage: NSImage(cgImage: cgImage, size: size))
                                .interpolation(.high)
                                .resizable()
                                .frame(width: leftWidth, height: height)
                        }
                        ForEach(Array(items.enumerated()), id: \.element.id) { index, paragraph in
                            let rect = session.pointRect(paragraph)
                            Rectangle()
                                .fill(lensAccent.opacity(hovered == paragraph.id ? 0.14 : 0.04))
                                .overlay(alignment: .leading) { Rectangle().fill(lensAccent.opacity(0.7)).frame(width: 2.5) }
                                .frame(width: rect.width * scale + 6, height: rect.height * scale + 4)
                                .offset(x: rect.minX * scale - 4, y: rect.minY * scale - 2)
                                .allowsHitTesting(false)
                            Text("\(index + 1)")
                                .font(.system(size: 9, weight: .bold, design: .rounded))
                                .foregroundStyle(.white)
                                .frame(width: 16, height: 16)
                                .background(Circle().fill(lensAccent))
                                .offset(x: max(0, rect.minX * scale - 20), y: rect.minY * scale - 1)
                        }
                    }
                    .frame(width: leftWidth, height: height, alignment: .topLeading)
                    .clipped()
                    .shadow(color: .black.opacity(0.08), radius: 4, y: 1)

                    MarginColumn(items: items, scale: scale, session: session, hovered: $hovered)
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                .padding(18)
            }
        }
    }
}

/// Translation cards placed at their paragraph's height, pushed down only when they would overlap.
struct MarginColumn: View {
    let items: [Paragraph]
    let scale: CGFloat
    @ObservedObject var session: LensSession
    @Binding var hovered: Int?

    var body: some View {
        MarginLayout(anchors: items.map { session.pointRect($0).minY * scale }) {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, paragraph in
                HStack(alignment: .top, spacing: 8) {
                    Text("\(index + 1)")
                        .font(.system(size: 9, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                        .frame(width: 16, height: 16)
                        .background(Circle().fill(lensAccent))
                    TranslationText(paragraph: paragraph, size: 13)
                }
                .padding(.vertical, 5)
                .padding(.horizontal, 8)
                .background(
                    RoundedRectangle(cornerRadius: 7)
                        .fill(hovered == paragraph.id ? lensAccent.opacity(0.08) : Color.clear)
                )
                .onHover { inside in hovered = inside ? paragraph.id : (hovered == paragraph.id ? nil : hovered) }
            }
        }
    }
}

struct MarginLayout: Layout {
    let anchors: [CGFloat]
    var spacing: CGFloat = 6

    private func positions(_ subviews: Subviews, width: CGFloat) -> ([CGFloat], CGFloat) {
        var y: [CGFloat] = []
        var cursor: CGFloat = 0
        for (index, view) in subviews.enumerated() {
            let height = view.sizeThatFits(ProposedViewSize(width: width, height: nil)).height
            let top = max(index < anchors.count ? anchors[index] : cursor, cursor)
            y.append(top)
            cursor = top + height + spacing
        }
        return (y, cursor)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 320
        return CGSize(width: width, height: positions(subviews, width: width).1)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let (y, _) = positions(subviews, width: bounds.width)
        for (index, view) in subviews.enumerated() {
            view.place(
                at: CGPoint(x: bounds.minX, y: bounds.minY + y[index]),
                proposal: ProposedViewSize(width: bounds.width, height: nil)
            )
        }
    }
}
