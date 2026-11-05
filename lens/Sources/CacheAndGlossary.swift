import CryptoKit
import Foundation
import SQLite3

enum AppPaths {
    static let supportDir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("Queqiao", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    static let localCacheDB = supportDir.appendingPathComponent("lens-cache.sqlite3")
    static let historyFile = supportDir.appendingPathComponent("lens-history.json")
    static let sharedQueqiaoCacheDB = URL(fileURLWithPath: "/Users/huia/codex/queqiao/data/cache.sqlite3")
    static let sharedGlossaryCSV = URL(fileURLWithPath: "/Users/huia/codex/queqiao/glossary/ai.csv")
}

/// Persistent translation cache backed by SQLite, compatible with Queqiao's `translations` table.
final class TranslationCache: @unchecked Sendable {
    static let shared = TranslationCache()

    private let lock = NSLock()
    private var memory: [String: String] = [:]
    private var localDB: OpaquePointer?
    private var sharedDB: OpaquePointer?

    private init() {
        localDB = Self.openDB(at: AppPaths.localCacheDB)
        if FileManager.default.fileExists(atPath: AppPaths.sharedQueqiaoCacheDB.path) {
            sharedDB = Self.openDB(at: AppPaths.sharedQueqiaoCacheDB)
        }
    }

    private static func openDB(at url: URL) -> OpaquePointer? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(url.path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            return nil
        }
        sqlite3_exec(db, "PRAGMA journal_mode=WAL;", nil, nil, nil)
        sqlite3_exec(
            db,
            "CREATE TABLE IF NOT EXISTS translations (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL);",
            nil,
            nil,
            nil
        )
        return db
    }

    static func makeKey(model: String, text: String) -> String {
        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let raw = "lens-v1\0\(model)\0\(normalized)"
        let digest = SHA256.hash(data: Data(raw.utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    func get(model: String, text: String) -> String? {
        let key = Self.makeKey(model: model, text: text)
        lock.lock()
        defer { lock.unlock() }
        if let hit = memory[key] { return hit }
        if let hit = Self.query(db: localDB, key: key) ?? Self.query(db: sharedDB, key: key) {
            memory[key] = hit
            return hit
        }
        return nil
    }

    func put(model: String, text: String, translation: String) {
        let trimmed = translation.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let key = Self.makeKey(model: model, text: text)
        lock.lock()
        defer { lock.unlock() }
        memory[key] = trimmed
        Self.insert(db: localDB, key: key, value: trimmed)
        Self.insert(db: sharedDB, key: key, value: trimmed)
    }

    private static func query(db: OpaquePointer?, key: String) -> String? {
        guard let db else { return nil }
        var stmt: OpaquePointer?
        defer { sqlite3_finalize(stmt) }
        let sql = "SELECT value FROM translations WHERE key = ? LIMIT 1;"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        sqlite3_bind_text(stmt, 1, (key as NSString).utf8String, -1, nil)
        guard sqlite3_step(stmt) == SQLITE_ROW, let cStr = sqlite3_column_text(stmt, 0) else { return nil }
        return String(cString: cStr)
    }

    private static func insert(db: OpaquePointer?, key: String, value: String) {
        guard let db else { return }
        var stmt: OpaquePointer?
        defer { sqlite3_finalize(stmt) }
        let sql = "INSERT OR REPLACE INTO translations (key, value, created) VALUES (?, ?, ?);"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return }
        sqlite3_bind_text(stmt, 1, (key as NSString).utf8String, -1, nil)
        sqlite3_bind_text(stmt, 2, (value as NSString).utf8String, -1, nil)
        sqlite3_bind_double(stmt, 3, Date().timeIntervalSince1970)
        _ = sqlite3_step(stmt)
    }
}

/// Loads Queqiao's `glossary/ai.csv` so screen and selection translations use consistent terminology.
enum Glossary {
    static let entries: [(source: String, target: String)] = load()

    private static func load() -> [(String, String)] {
        var urls: [URL] = []
        if let bundled = Bundle.main.url(forResource: "ai", withExtension: "csv") {
            urls.append(bundled)
        }
        urls.append(AppPaths.sharedGlossaryCSV)
        for url in urls {
            guard let content = try? String(contentsOf: url, encoding: .utf8) else { continue }
            var list: [(String, String)] = []
            for (index, rawLine) in content.components(separatedBy: .newlines).enumerated() {
                let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
                if line.isEmpty || (index == 0 && line.lowercased().hasPrefix("source,")) { continue }
                let parts = line.split(separator: ",", maxSplits: 1).map { String($0).trimmingCharacters(in: .whitespaces) }
                if parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty {
                    list.append((parts[0], parts[1]))
                }
            }
            if !list.isEmpty { return list }
        }
        return []
    }

    static func promptInstruction(for text: String) -> String {
        guard LensSettings.useGlossary, !entries.isEmpty else { return "" }
        let lower = text.lowercased()
        let matched = entries.filter { lower.contains($0.source.lowercased()) }.prefix(24)
        guard !matched.isEmpty else { return "" }
        let pairs = matched.map { "\($0.source) → \($0.target)" }.joined(separator: "；")
        return "\n术语参考（出现时优先采用）：\(pairs)。"
    }
}

struct HistoryItem: Codable, Identifiable {
    struct Pair: Codable, Identifiable {
        var id: Int
        var original: String
        var translation: String
    }

    var id: String
    var timestamp: Date
    var sourceKind: String
    var model: String
    var pairs: [Pair]

    var title: String {
        let first = pairs.first?.original.trimmingCharacters(in: .whitespacesAndNewlines) ?? "空记录"
        return String(first.prefix(48))
    }

    var relativeTime: String {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "zh-CN")
        formatter.unitsStyle = .short
        return formatter.localizedString(for: timestamp, relativeTo: Date())
    }
}

@MainActor
final class HistoryStore: ObservableObject {
    static let shared = HistoryStore()
    @Published private(set) var items: [HistoryItem] = []
    private let maxItems = 30

    private init() {
        load()
    }

    func record(session: LensSession) {
        let pairs = session.paragraphs.compactMap { p -> HistoryItem.Pair? in
            guard !p.skip, let t = p.translation, !t.isEmpty, !p.failed else { return nil }
            return HistoryItem.Pair(id: p.id, original: p.text, translation: t)
        }
        guard !pairs.isEmpty else { return }
        let entry = HistoryItem(
            id: session.id,
            timestamp: Date(),
            sourceKind: session.isSelectionOnly ? "划词" : "截屏",
            model: session.model,
            pairs: pairs
        )
        items.removeAll { $0.id == entry.id }
        items.insert(entry, at: 0)
        if items.count > maxItems { items = Array(items.prefix(maxItems)) }
        save()
    }

    func clear() {
        items.removeAll()
        save()
    }

    private func load() {
        guard let data = try? Data(contentsOf: AppPaths.historyFile),
              let decoded = try? JSONDecoder().decode([HistoryItem].self, from: data)
        else { return }
        items = decoded
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(items) else { return }
        try? data.write(to: AppPaths.historyFile, options: .atomic)
    }
}
