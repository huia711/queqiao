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

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            if let error = session.error, session.paragraphs.isEmpty {
                message(error, systemImage: "exclamationmark.triangle")
            } else if session.paragraphs.isEmpty {
                message(session.status, systemImage: nil)
            } else {
                switch mode {
                case .bilingual: BilingualView(session: session)
                case .margin: MarginView(session: session)
                }
            }
        }
        .frame(minWidth: 480, minHeight: 320)
        .background(Color(nsColor: .textBackgroundColor))
    }

    private var toolbar: some View {
        HStack(spacing: 12) {
            Picker("", selection: $mode) {
                ForEach(ViewMode.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 150)
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
            Menu("复制") {
                Button("复制译文") { copy(false) }
                Button("复制原文 + 译文") { copy(true) }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .disabled(!session.finished || session.paragraphs.isEmpty)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
    }

    private func copy(_ bilingual: Bool) {
        session.copy(bilingual: bilingual)
        copied = "已复制"
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
            Text(text)
                .font(.system(size: size))
                .foregroundStyle(paragraph.failed ? Color.red : Color.primary)
                .lineSpacing(3)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
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
                            Text(paragraph.text).font(.system(size: 13)).foregroundStyle(.secondary)
                        }
                        if !paragraph.skip {
                            TranslationText(paragraph: paragraph)
                                .padding(.leading, 10)
                                .overlay(alignment: .leading) {
                                    Rectangle().fill(lensAccent.opacity(0.55)).frame(width: 2.5)
                                }
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
                        Image(nsImage: NSImage(cgImage: session.image, size: size))
                            .interpolation(.high)
                            .resizable()
                            .frame(width: leftWidth, height: height)
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
