import AppKit
import CryptoKit
import SwiftUI
import UniformTypeIdentifiers

private let ink = Color(red: 0.12, green: 0.17, blue: 0.17)
private let teal = Color(red: 0.00, green: 0.36, blue: 0.33)
private let paper = Color(red: 0.97, green: 0.97, blue: 0.94)
private let line = Color(red: 0.87, green: 0.89, blue: 0.85)

struct Change: Decodable, Identifiable, Equatable {
    let id: Int
    let path: String
    let event: String
    let ts: Double
    let restored: Bool
    let isRestore: Bool

    var title: String { URL(fileURLWithPath: path).lastPathComponent }
    var date: Date { Date(timeIntervalSince1970: ts) }
    var label: String {
        if isRestore { return "恢复" }
        switch event {
        case "created": return "新增"
        case "deleted": return "删除"
        default: return "修改"
        }
    }
    var symbol: String {
        if isRestore { return "arrow.uturn.backward" }
        switch event {
        case "created": return "plus"
        case "deleted": return "minus"
        default: return "pencil"
        }
    }
}

private struct MainActionStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 13, weight: .semibold))
            .padding(.horizontal, 15).padding(.vertical, 9)
            .foregroundStyle(.white)
            .background(teal.opacity(enabled ? (configuration.isPressed ? 0.76 : 1) : 0.45),
                        in: RoundedRectangle(cornerRadius: 8))
    }
}

struct Preview: Decodable {
    let path: String
    let before: String?
    let current: String?
    let token: String?
    let message: String?
}

struct Audit: Decodable {
    struct Claim: Decodable {
        let subject: String
        let kind: String
        let status: String
        let detail: String
        var statusLabel: String {
            switch status {
            case "ok": return "已核对"
            case "failed": return "存在矛盾"
            default: return "证据不足"
            }
        }
        var actionLabel: String {
            switch kind {
            case "deleted": return "AI 声称删除了此文件"
            case "created": return "AI 声称创建了此文件"
            case "edited": return "AI 声称修改了此文件"
            case "installed": return "AI 声称安装了此依赖"
            default: return "AI 在对话里提到了这项操作"
            }
        }
    }
    struct Finding: Decodable {
        let severity: String
        let title: String
        let detail: String
    }
    let claims: [Claim]
    let findings: [Finding]
    let sessions: Int
    let warnings: [String]
}

struct Snapshot: Decodable {
    let ready: Bool
    let events: [Change]
    let root: String?
    let state: String?
    let transcript: String?
}

struct Workspace: Equatable {
    let root: String
    let state: String
    var demoTranscript: String? = nil
    var name: String { URL(fileURLWithPath: root).lastPathComponent }
    var isDemo: Bool { demoTranscript != nil }

    func args(_ action: String, _ extra: [String] = []) -> [String] {
        [action, root, "--state", state] + extra
    }
}

private struct EngineError: LocalizedError {
    let errorDescription: String?
    init(_ text: String) { errorDescription = text }
}

private enum Engine {
    struct Failure: Decodable { let error: String }
    struct Restored: Decodable { let path: String }

    static func process(_ arguments: [String]) throws -> (Process, Pipe) {
        guard let directory = Bundle.main.resourceURL else { throw EngineError("找不到应用资源。") }
        let executable = directory.appendingPathComponent("engine/changes-engine")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw EngineError("找不到文件记录组件。请重新解压完整的应用。")
        }
        let process = Process()
        let output = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        return (process, output)
    }

    static func failure(_ output: Data, fallback: String) -> EngineError {
        EngineError((try? JSONDecoder().decode(Failure.self, from: output).error) ?? fallback)
    }

    static func request<T: Decodable & Sendable>(_ type: T.Type, _ arguments: [String]) async throws -> T {
        try await Task.detached(priority: .userInitiated) {
            let (process, output) = try Self.process(arguments)
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                throw failure(data, fallback: "操作没有完成。请稍后再试。")
            }
            return try JSONDecoder().decode(type, from: data)
        }.value
    }
}

@MainActor
private final class WatchProcess {
    static let shared = WatchProcess()
    private var process: Process?

    func start(_ workspace: Workspace, finished: @escaping (String?) -> Void) throws {
        stop()
        let (process, output) = try Engine.process(workspace.args("watch"))
        self.process = process
        process.terminationHandler = { [weak self] process in
            let message = process.terminationStatus == 0 ? nil : Engine.failure(
                output.fileHandleForReading.readDataToEndOfFile(), fallback: "文件记录已停止。"
            ).errorDescription
            DispatchQueue.main.async { [weak self] in
                guard self?.process === process else { return }
                self?.process = nil
                finished(message)
            }
        }
        do { try process.run() } catch {
            self.process = nil
            throw error
        }
    }

    func stop() {
        let previous = process
        process = nil
        if let previous, previous.isRunning { previous.terminate() }
    }
}

@MainActor
final class DeskModel: ObservableObject {
    @Published var workspace: Workspace?
    @Published var changes: [Change] = []
    @Published var selection: Int?
    @Published var preview: Preview?
    @Published var audit: Audit?
    @Published var recording = false
    @Published var ready = false
    @Published var busy = false
    @Published var message: String?
    @Published var error: String?

    private var storage: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("File Change Desk", isDirectory: true)
    }

    private func reset(_ folder: Workspace) {
        WatchProcess.shared.stop()
        workspace = folder
        changes = []
        preview = nil
        selection = nil
        audit = nil
        recording = false
        ready = false
        message = nil
    }

    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "选择要记录的文件夹"
        panel.prompt = "开始记录"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        guard panel.runModal() == .OK, let directory = panel.url else { return }
        let root = directory.resolvingSymlinksInPath().standardizedFileURL.path
        let hash = SHA256.hash(data: Data(root.utf8)).map { String(format: "%02x", Int($0)) }.joined()
        reset(Workspace(root: root, state: storage.appendingPathComponent("folders/" + hash).path))
        startRecording()
    }

    func startRecording() {
        guard let workspace, !workspace.isDemo else { return }
        do {
            try WatchProcess.shared.start(workspace) { [weak self] error in
                self?.recording = false
                self?.error = error
            }
            recording = true
        } catch { self.error = error.localizedDescription }
    }

    func stopRecording() {
        WatchProcess.shared.stop()
        recording = false
    }

    func demo() async {
        busy = true
        defer { busy = false }
        do {
            let result = try await Engine.request(Snapshot.self, ["demo", storage.path])
            guard let root = result.root, let state = result.state, let transcript = result.transcript else {
                throw EngineError("演示文件没有准备好。")
            }
            reset(Workspace(root: root, state: state, demoTranscript: transcript))
            changes = result.events
            ready = result.ready
            selection = changes.first?.id
        } catch { self.error = error.localizedDescription }
    }

    func refresh(_ folder: Workspace) async {
        do {
            let result = try await Engine.request(Snapshot.self, folder.args("changes"))
            guard workspace == folder, !Task.isCancelled else { return }
            if changes != result.events { changes = result.events }
            ready = result.ready
        } catch {
            guard workspace == folder, !Task.isCancelled else { return }
            if !recording { self.error = error.localizedDescription }
        }
    }

    func readPreview() async {
        guard let folder = workspace, let event = selection else { return }
        preview = nil
        do {
            let result = try await Engine.request(Preview.self, folder.args("preview", ["--event", String(event)]))
            guard workspace == folder, selection == event, !Task.isCancelled else { return }
            preview = result
        } catch {
            guard workspace == folder, selection == event, !Task.isCancelled else { return }
            self.error = error.localizedDescription
        }
    }

    func restore() async {
        guard let folder = workspace, let event = selection, let token = preview?.token else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await Engine.request(
                Engine.Restored.self, folder.args("restore", ["--event", String(event), "--token", token]))
            await refresh(folder)
            await readPreview()
            message = "已恢复 \(URL(fileURLWithPath: result.path).lastPathComponent)"
        } catch {
            self.error = error.localizedDescription
            await readPreview()
        }
    }

    func chooseTranscript() async {
        guard let workspace else { return }
        let path: String
        if let example = workspace.demoTranscript {
            path = example
        } else {
            let panel = NSOpenPanel()
            panel.title = "选择 AI 对话日志"
            panel.prompt = "核对这份记录"
            panel.canChooseDirectories = false
            panel.canChooseFiles = true
            panel.allowedContentTypes = [.data]
            guard panel.runModal() == .OK, let url = panel.url else { return }
            path = url.path
        }
        busy = true
        defer { busy = false }
        do {
            audit = try await Engine.request(Audit.self, workspace.args("audit", ["--transcript", path]))
        } catch { self.error = error.localizedDescription }
    }
}

private enum DeskTab { case changes, audit }

struct DeskView: View {
    @StateObject private var model = DeskModel()
    @State private var tab: DeskTab = .changes
    @State private var confirmRestore = false
    @State private var filter = ""

    var body: some View {
        VStack(spacing: 0) {
            if let folder = model.workspace { desk(folder) } else { home }
        }
        .frame(minWidth: 900, minHeight: 580)
        .foregroundStyle(ink)
        .background(paper)
        .tint(teal)
        .preferredColorScheme(.light)
        .task(id: model.workspace) {
            guard let folder = model.workspace, !folder.isDemo else { return }
            while !Task.isCancelled {
                await model.refresh(folder)
                try? await Task.sleep(for: .seconds(1.5))
            }
        }
        .task(id: (model.workspace?.root ?? "") + ":" + (model.selection.map(String.init) ?? "")) {
            await model.readPreview()
        }
        .alert("恢复到改动前？", isPresented: $confirmRestore) {
            Button("取消", role: .cancel) {}
            Button("确认恢复") { Task { await model.restore() } }
        } message: {
            Text("将使用左侧的旧版本替换 \(model.preview?.path ?? "该文件")。恢复也会进入改动记录。")
        }
        .alert("操作未完成", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("好", role: .cancel) { model.error = nil }
        } message: { Text(model.error ?? "") }
    }

    private var home: some View {
        VStack(alignment: .leading, spacing: 23) {
            Image(systemName: "clock.arrow.circlepath")
                .font(.system(size: 42, weight: .medium)).foregroundStyle(teal)
                .frame(width: 82, height: 82)
                .background(Color.white, in: RoundedRectangle(cornerRadius: 22))
            VStack(alignment: .leading, spacing: 13) {
                Text("文件改动记录").font(.system(size: 46, weight: .semibold))
                Text("看看文件被改了什么，\n需要时恢复到改动前。")
                    .font(.system(size: 21)).foregroundStyle(.secondary).lineSpacing(6)
            }
            HStack(spacing: 14) {
                Button("试用演示") { Task { await model.demo() } }
                    .buttonStyle(MainActionStyle()).accessibilityIdentifier("tryDemo")
                Button("选择文件夹…") { model.chooseFolder() }.buttonStyle(.bordered)
            }
            .controlSize(.large).disabled(model.busy)
            if model.busy { ProgressView().controlSize(.small) }
            else { Text("演示使用自带的样例文件。").font(.callout).foregroundStyle(.secondary) }
        }
        .frame(maxWidth: 520, alignment: .leading)
        .padding(44)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func desk(_ folder: Workspace) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 14) {
                Image(systemName: folder.isDemo ? "sparkle.magnifyingglass" : "folder")
                    .font(.system(size: 20, weight: .medium)).foregroundStyle(teal)
                VStack(alignment: .leading, spacing: 4) {
                    Text(folder.name).font(.system(size: 18, weight: .semibold))
                    Text(folder.isDemo ? "样例 · 可放心操作" : folder.root)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                if !folder.isDemo {
                    Label(model.recording ? (model.ready ? "正在记录" : "正在建立基线") : "已暂停",
                          systemImage: model.recording ? "record.circle" : "pause.circle")
                        .foregroundStyle(model.recording ? teal : .secondary).font(.callout)
                    Button(model.recording ? "暂停" : "开始记录") {
                        model.recording ? model.stopRecording() : model.startRecording()
                    }
                }
                Button("换文件夹…") { tab = .changes; filter = ""; model.chooseFolder() }
            }
            .padding(.horizontal, 24).padding(.vertical, 16).disabled(model.busy)

            HStack(spacing: 8) {
                tabButton("文件改动", symbol: "doc.text.magnifyingglass", value: .changes)
                tabButton("核对 AI", symbol: "checkmark.bubble", value: .audit)
                Spacer()
                if let message = model.message {
                    Label(message, systemImage: "checkmark.circle.fill")
                        .font(.callout).foregroundStyle(teal).lineLimit(1)
                        .accessibilityIdentifier("restoreResult")
                }
            }.padding(.horizontal, 24).padding(.bottom, 12)
            Divider()
            if tab == .changes { changeDesk } else { auditDesk(folder) }
        }
    }

    private func tabButton(_ title: String, symbol: String, value: DeskTab) -> some View {
        Button { tab = value } label: {
            Label(title, systemImage: symbol).padding(.horizontal, 12).padding(.vertical, 8)
                .foregroundStyle(tab == value ? teal : ink)
                .background(tab == value ? Color.white : .clear, in: RoundedRectangle(cornerRadius: 8))
        }.buttonStyle(.plain)
    }

    private var changeDesk: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                TextField("搜索文件名", text: $filter).textFieldStyle(.roundedBorder).padding(14)
                if model.changes.isEmpty {
                    Text(model.recording && !model.ready ? "正在读取文件夹…" : "还没有记录到文件改动")
                        .font(.callout).foregroundStyle(.secondary).padding(14)
                }
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 5) {
                        ForEach(model.changes.filter { filter.isEmpty || $0.path.localizedCaseInsensitiveContains(filter) }) { event in
                            Button { model.selection = event.id } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: event.symbol).foregroundStyle(teal).frame(width: 22)
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(event.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                                        Text("\(event.label) · \(event.date.formatted(date: .omitted, time: .shortened))")
                                            .font(.caption).foregroundStyle(.secondary)
                                        if event.restored && !event.isRestore {
                                            Text("已恢复").font(.caption2).foregroundStyle(teal)
                                        }
                                    }
                                    Spacer()
                                }.padding(9).background(model.selection == event.id ? Color.white : .clear,
                                                          in: RoundedRectangle(cornerRadius: 8))
                            }.buttonStyle(.plain)
                        }
                    }.padding(.horizontal, 9)
                }
            }.frame(width: 242)
            Divider()
            detail.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color.white)
        }
    }

    @ViewBuilder private var detail: some View {
        if let preview = model.preview {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Text(preview.path).font(.title2.weight(.semibold)).lineLimit(2)
                    Spacer()
                    if preview.token != nil && preview.before != preview.current {
                        Button("恢复到改动前…") { confirmRestore = true }
                            .buttonStyle(MainActionStyle()).disabled(model.busy)
                            .accessibilityIdentifier("restoreVersion")
                    } else if preview.token != nil {
                        Label("当前已是此版本", systemImage: "checkmark.circle").foregroundStyle(teal)
                    }
                }
                if let before = preview.before {
                    HStack(alignment: .top, spacing: 12) {
                        textPanel("改动前", text: before, other: preview.current ?? "", isCurrent: false)
                        textPanel("当前文件", text: preview.current ?? "文件当前不存在",
                                  other: before, isCurrent: true)
                    }
                } else {
                    Text(preview.message ?? "暂无预览").foregroundStyle(.secondary)
                    Spacer()
                }
            }.padding(24)
        } else {
            VStack(spacing: 10) {
                Image(systemName: "doc.text.magnifyingglass").font(.system(size: 34)).foregroundStyle(teal)
                Text(model.selection == nil ? "选一条改动查看内容" : "正在读取预览…").foregroundStyle(.secondary)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func textPanel(_ title: String, text: String, other: String, isCurrent: Bool) -> some View {
        let own = text.components(separatedBy: "\n")
        let comparison = other.components(separatedBy: "\n")
        return VStack(alignment: .leading, spacing: 0) {
            Text(title).font(.callout.weight(.medium)).padding(12)
            Divider()
            ScrollView([.horizontal, .vertical]) {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(own.indices.prefix(1000)), id: \.self) { i in
                        let changed = i >= comparison.count || own[i] != comparison[i]
                        HStack(alignment: .top, spacing: 9) {
                            Text("\(i + 1)").foregroundStyle(.secondary).frame(minWidth: 26, alignment: .trailing)
                            Text(own[i].isEmpty ? " " : own[i]).fixedSize(horizontal: true, vertical: false)
                                .textSelection(.enabled)
                        }
                        .font(.system(size: 13, design: .monospaced)).padding(.vertical, 3).padding(.trailing, 10)
                        .background(changed ? (isCurrent ? Color.orange.opacity(0.14) : teal.opacity(0.10)) : .clear,
                                    in: RoundedRectangle(cornerRadius: 4))
                    }
                    if own.count > 1000 {
                        Text("预览显示前 1,000 行。恢复会使用完整旧版本。")
                            .font(.caption).foregroundStyle(.secondary).padding(8)
                    }
                }.padding(10)
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(paper.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(line))
    }

    private func auditDesk(_ folder: Workspace) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("核对 AI 的说法").font(.title2.weight(.semibold))
                Text(folder.isDemo ? "样例里的 AI 说它删了旅行计划。看看文件和记录怎么说。"
                    : "选择一份本地对话日志，核对 AI 提到的文件改动。")
                    .foregroundStyle(.secondary)
                Button(folder.isDemo ? "核对演示" : "选择对话日志…") {
                    Task { await model.chooseTranscript() }
                }.buttonStyle(MainActionStyle()).disabled(model.busy)
                if let report = model.audit {
                    if report.claims.isEmpty {
                        Text(report.sessions > 0 ? "这份日志里没有读到可核对的声明。" : "未识别到对话记录。")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(Array(report.claims.enumerated()), id: \.offset) { _, claim in
                        VStack(alignment: .leading, spacing: 6) {
                            Text("\(claim.statusLabel) · \(claim.subject)").font(.headline)
                                .foregroundStyle(claim.status == "failed" ? Color(red: 0.56, green: 0.23, blue: 0.09) : teal)
                            Text(claim.actionLabel).font(.callout)
                            Text(claim.detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(13)
                            .background(paper, in: RoundedRectangle(cornerRadius: 8))
                    }
                    ForEach(Array(report.findings.enumerated()), id: \.offset) { _, finding in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(finding.title).font(.callout.bold())
                            Text(finding.detail).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                    }
                    ForEach(report.warnings, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
                }
            }.frame(maxWidth: 690, alignment: .leading).padding(30)
                .frame(maxWidth: .infinity, alignment: .leading)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color.white)
    }
}

@MainActor
final class AppLifecycle: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { WatchProcess.shared.stop() }
}

@main struct ChangeDeskApp: App {
    @NSApplicationDelegateAdaptor(AppLifecycle.self) private var lifecycle

    var body: some Scene {
        WindowGroup("文件改动记录") { DeskView() }
            .defaultSize(width: 1040, height: 680)
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}
