import Cocoa
import AVFoundation
import Carbon.HIToolbox

// =============================================================================
// Config
// =============================================================================
let kDoubleTapIntervalMs: Double = 300
let kMaxTapHoldMs: Double = 200
let kSampleRate: Double = 16000
let kMaxRecordingDuration: Double = 600
let kWhisperModel = "whisper-1"
let kTranslateModel = "gpt-4o-mini"

// =============================================================================
// API Key Management
// =============================================================================
func getEnvPath() -> String {
    // ~/.config/voice-to-text/.env
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    return (home as NSString).appendingPathComponent(".config/voice-to-text/.env")
}

func loadApiKey() -> String {
    // 1) .env dosyasindan
    let envPath = getEnvPath()
    if let content = try? String(contentsOfFile: envPath, encoding: .utf8) {
        for line in content.components(separatedBy: "\n") {
            if line.hasPrefix("OPENAI_API_KEY=") {
                return String(line.dropFirst("OPENAI_API_KEY=".count)).trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
    }
    // 2) Environment variable
    if let key = ProcessInfo.processInfo.environment["OPENAI_API_KEY"], !key.isEmpty {
        return key
    }
    // 3) Working directory .env
    let wdEnv = FileManager.default.currentDirectoryPath + "/.env"
    if let content = try? String(contentsOfFile: wdEnv, encoding: .utf8) {
        for line in content.components(separatedBy: "\n") {
            if line.hasPrefix("OPENAI_API_KEY=") {
                return String(line.dropFirst("OPENAI_API_KEY=".count)).trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
    }
    return ""
}

// =============================================================================
// Audio Recorder
// =============================================================================
class AudioRecorder {
    private var audioEngine: AVAudioEngine?
    private var audioFile: AVAudioFile?
    private var isRecording = false
    private var tempURL: URL
    private(set) var currentLevel: Float = 0

    init() {
        tempURL = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("vtt_recording.wav")
    }

    func startRecording() {
        guard !isRecording else { return }

        let engine = AVAudioEngine()
        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        // WAV dosyasi olustur
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: kSampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
        ]
        do {
            audioFile = try AVAudioFile(forWriting: tempURL, settings: settings)
        } catch {
            print("Audio file olusturulamadi: \(error)")
            return
        }

        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self = self, let file = self.audioFile else { return }

            // Level hesapla
            let channelData = buffer.floatChannelData?[0]
            let count = Int(buffer.frameLength)
            if let data = channelData, count > 0 {
                var sum: Float = 0
                for i in 0..<count { sum += abs(data[i]) }
                self.currentLevel = min(sum / Float(count) * 10, 1.0)
            }

            // PCM convert & write
            guard let convertedBuffer = AVAudioPCMBuffer(
                pcmFormat: file.processingFormat,
                frameCapacity: AVAudioFrameCount(Double(buffer.frameLength) * kSampleRate / format.sampleRate)
            ) else { return }

            let converter = AVAudioConverter(from: format, to: file.processingFormat)
            var gotData = false
            do {
                try converter?.convert(to: convertedBuffer, error: nil) { _, status in
                    if !gotData {
                        gotData = true
                        status.pointee = .haveData
                        return buffer
                    }
                    status.pointee = .noDataNow
                    return nil
                }
                try file.write(from: convertedBuffer)
            } catch {
                // Sessiz devam
            }
        }

        do {
            try engine.start()
            audioEngine = engine
            isRecording = true
            print("Kayit basladi")
        } catch {
            print("Audio engine baslatilamadi: \(error)")
        }
    }

    func stopRecording() -> URL? {
        guard isRecording else { return nil }
        isRecording = false
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()
        audioEngine = nil
        audioFile = nil
        currentLevel = 0

        // Dosya var mi kontrol
        if FileManager.default.fileExists(atPath: tempURL.path) {
            print("Kayit durduruldu: \(tempURL.path)")
            return tempURL
        }
        return nil
    }

    func cleanup() {
        isRecording = false
        audioEngine?.stop()
        audioEngine = nil
        audioFile = nil
    }
}

// =============================================================================
// OpenAI API
// =============================================================================
class OpenAIClient {
    let apiKey: String

    init(apiKey: String) {
        self.apiKey = apiKey
    }

    func transcribe(fileURL: URL, completion: @escaping (String?) -> Void) {
        let url = URL(string: "https://api.openai.com/v1/audio/transcriptions")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")

        let boundary = UUID().uuidString
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()
        // model
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"model\"\r\n\r\n".data(using: .utf8)!)
        body.append("\(kWhisperModel)\r\n".data(using: .utf8)!)
        // file
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: audio/wav\r\n\r\n".data(using: .utf8)!)
        if let fileData = try? Data(contentsOf: fileURL) {
            body.append(fileData)
        }
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)

        request.httpBody = body

        URLSession.shared.dataTask(with: request) { data, _, error in
            if let error = error {
                print("Transcribe hata: \(error)")
                completion(nil)
                return
            }
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let text = json["text"] as? String else {
                if let data = data, let str = String(data: data, encoding: .utf8) {
                    print("API cevabi: \(str)")
                }
                completion(nil)
                return
            }
            completion(text)
        }.resume()
    }

    func translate(text: String, to targetLang: String, completion: @escaping (String?) -> Void) {
        let langNames = ["tr": "Turkish", "ru": "Russian", "en": "English"]
        let targetName = langNames[targetLang] ?? "Turkish"

        let url = URL(string: "https://api.openai.com/v1/chat/completions")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = [
            "model": kTranslateModel,
            "messages": [
                ["role": "system", "content": "You are a translator. Translate the given text to \(targetName). Only output the translation, nothing else. If the text is already in \(targetName), return it as-is."],
                ["role": "user", "content": text],
            ],
            "max_tokens": 1000,
            "temperature": 0.3,
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: request) { data, _, error in
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let choices = json["choices"] as? [[String: Any]],
                  let message = choices.first?["message"] as? [String: Any],
                  let content = message["content"] as? String else {
                completion(nil)
                return
            }
            completion(content.trimmingCharacters(in: .whitespacesAndNewlines))
        }.resume()
    }
}

// =============================================================================
// Floating Indicator Window — Figma v3.0 tasarimi
// =============================================================================
class GradientPillView: NSView {
    private var blurView: NSVisualEffectView!

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.cornerRadius = frame.height / 2
        layer?.masksToBounds = true

        // 1) Behind-window blur — arkadaki icerigi cam gibi gosterir
        blurView = NSVisualEffectView(frame: bounds)
        blurView.material = .hudWindow
        blurView.blendingMode = .behindWindow
        blurView.state = .active
        blurView.autoresizingMask = [.width, .height]
        addSubview(blurView)

        // Tint yok — saf cam efekti (Dock gibi)

        // 3) Ust kenarda ince specular highlight — isik yansimasi
        let highlight = CAGradientLayer()
        highlight.frame = CGRect(x: 0, y: bounds.height - 1, width: bounds.width, height: 1)
        highlight.colors = [
            NSColor(white: 1, alpha: 0).cgColor,
            NSColor(white: 1, alpha: 0.25).cgColor,
            NSColor(white: 1, alpha: 0).cgColor,
        ]
        highlight.startPoint = CGPoint(x: 0, y: 0.5)
        highlight.endPoint = CGPoint(x: 1, y: 0.5)
        blurView.layer?.addSublayer(highlight)

        // 4) Cam kenari — ince parlak border
        let border = CAShapeLayer()
        let pillPath = CGPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5),
                              cornerWidth: frame.height / 2, cornerHeight: frame.height / 2, transform: nil)
        border.path = pillPath
        border.fillColor = nil
        border.strokeColor = NSColor(white: 1, alpha: 0.12).cgColor
        border.lineWidth = 1
        blurView.layer?.addSublayer(border)

        // 5) Inner shadow — derinlik efekti
        let innerShadow = CALayer()
        innerShadow.frame = bounds
        innerShadow.cornerRadius = frame.height / 2
        innerShadow.shadowColor = NSColor.black.cgColor
        innerShadow.shadowOffset = CGSize(width: 0, height: -2)
        innerShadow.shadowRadius = 6
        innerShadow.shadowOpacity = 0.4
        innerShadow.masksToBounds = true
        // Shadow path iceriden
        let outerPath = CGMutablePath()
        outerPath.addRect(bounds.insetBy(dx: -20, dy: -20))
        outerPath.addRoundedRect(in: bounds, cornerWidth: frame.height / 2, cornerHeight: frame.height / 2)
        innerShadow.shadowPath = outerPath
        blurView.layer?.addSublayer(innerShadow)
    }
    required init?(coder: NSCoder) { fatalError() }
}

class WaveformView: NSView {
    private var levels: [CGFloat] = Array(repeating: 0.02, count: 24)

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError() }

    func addLevel(_ level: Float) {
        levels.removeFirst()
        levels.append(CGFloat(max(level, 0.02)))
        needsDisplay = true
    }

    func reset() {
        levels = Array(repeating: 0.02, count: 24)
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let barW: CGFloat = 1.5
        let gap: CGFloat = 2
        let cy = bounds.height / 2

        for (i, level) in levels.enumerated() {
            let h = max(3, level * bounds.height * 0.85)
            let x = CGFloat(i) * (barW + gap)
            let brightness = 0.75 + 0.20 * min(level / 0.4, 1)
            NSColor(white: brightness, alpha: 1).setFill()
            let rect = NSRect(x: x, y: cy - h/2, width: barW, height: h)
            NSBezierPath(roundedRect: rect, xRadius: 1.5, yRadius: 1.5).fill()
        }
    }
}

class IndicatorWindow: NSWindow {
    var selectedLang = "tr"
    var onCancel: (() -> Void)?
    private var dotView: NSView!
    private var timerLabel: NSTextField!
    private var langButtons: [String: NSButton] = [:]
    private var waveView: WaveformView!
    private var recordingTimer: Timer?
    private var startTime: Date?
    private var pulseTimer: Timer?

    init() {
        let w: CGFloat = 300
        let h: CGFloat = 38
        let screen = NSScreen.main!
        let x = (screen.frame.width - w) / 2
        let y: CGFloat = 80

        super.init(
            contentRect: NSRect(x: x, y: y, width: w, height: h),
            styleMask: .borderless,
            backing: .buffered,
            defer: false
        )

        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = true
        self.level = .floating
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.isMovableByWindowBackground = true

        let pillView = GradientPillView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        self.contentView = pillView

        let pad: CGFloat = 15
        var cx: CGFloat = pad

        // Kayit noktasi — 9px cap
        let dotR: CGFloat = 5
        dotView = NSView(frame: NSRect(x: cx, y: h/2 - dotR, width: dotR*2, height: dotR*2))
        dotView.wantsLayer = true
        dotView.layer = CALayer()
        dotView.layer?.backgroundColor = NSColor.systemRed.cgColor
        dotView.layer?.cornerRadius = dotR
        pillView.addSubview(dotView)
        cx += dotR * 2 + 10

        // Waveform — 24 bar
        let waveW: CGFloat = CGFloat(24) * 3.5
        waveView = WaveformView(frame: NSRect(x: cx, y: 5, width: waveW, height: h - 10))
        pillView.addSubview(waveView)
        cx += waveW + 10

        // Timer (5px sola)
        cx -= 5
        timerLabel = NSTextField(labelWithString: "0:00")
        timerLabel.frame = NSRect(x: cx, y: (h - 22) / 2, width: 44, height: 22)
        timerLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 18, weight: .light)
        timerLabel.textColor = NSColor(white: 0.75, alpha: 1)
        timerLabel.alignment = .center
        pillView.addSubview(timerLabel)
        cx += 48

        // Dil butonlari
        let btnSize: CGFloat = 24
        let btnGap: CGFloat = 6
        let langs = [("TR", "tr"), ("RU", "ru"), ("EN", "en")]

        for (idx, (label, code)) in langs.enumerated() {
            let bx = cx + CGFloat(idx) * (btnSize + btnGap)
            let btn = NSButton(frame: NSRect(x: bx, y: h/2 - btnSize/2, width: btnSize, height: btnSize))
            btn.title = label
            btn.font = NSFont.systemFont(ofSize: 12, weight: .light)
            btn.isBordered = false
            btn.wantsLayer = true
            btn.layer?.cornerRadius = 7
            btn.layer?.borderWidth = 0.8
            btn.layer?.backgroundColor = NSColor(red: 0.04, green: 0.04, blue: 0.04, alpha: 1).cgColor
            btn.target = self
            btn.action = #selector(langButtonClicked(_:))
            btn.tag = idx
            pillView.addSubview(btn)
            langButtons[code] = btn
        }
        cx += 3 * (btnSize + btnGap)
        updateLangButtons()

        // Ayirici cizgi
        let sep = NSView(frame: NSRect(x: cx, y: 8, width: 1, height: h - 16))
        sep.wantsLayer = true
        sep.layer = CALayer()
        sep.layer?.backgroundColor = NSColor(white: 0.23, alpha: 1).cgColor
        pillView.addSubview(sep)
        cx += 8

        // X butonu
        let xBtn = NSButton(frame: NSRect(x: cx, y: h/2 - 8, width: 17, height: 17))
        xBtn.title = "✕"
        xBtn.font = NSFont.systemFont(ofSize: 12, weight: .bold)
        xBtn.contentTintColor = NSColor(red: 1, green: 0.27, blue: 0.23, alpha: 1)
        xBtn.isBordered = false
        xBtn.target = self
        xBtn.action = #selector(cancelClicked)
        pillView.addSubview(xBtn)
    }

    @objc func langButtonClicked(_ sender: NSButton) {
        let langs = ["tr", "ru", "en"]
        selectedLang = langs[sender.tag]
        updateLangButtons()
    }

    @objc func cancelClicked() {
        onCancel?()
    }

    func updateLangButtons() {
        let green = NSColor(red: 0, green: 0.69, blue: 0.09, alpha: 1)
        let gray = NSColor(white: 0.36, alpha: 1)
        for (code, btn) in langButtons {
            let active = code == selectedLang
            btn.layer?.borderColor = active ? green.cgColor : gray.cgColor
            btn.contentTintColor = active ? green : gray
        }
    }

    func showRecording() {
        startTime = Date()
        dotView.layer?.backgroundColor = NSColor.systemRed.cgColor
        timerLabel.stringValue = "0:00"
        waveView.reset()

        recordingTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self = self, let start = self.startTime else { return }
            let elapsed = Int(Date().timeIntervalSince(start))
            self.timerLabel.stringValue = String(format: "%d:%02d", elapsed / 60, elapsed % 60)
        }

        pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            let alpha = 0.6 + 0.4 * sin(Date().timeIntervalSince1970 * 3)
            self.dotView.layer?.backgroundColor = NSColor.systemRed.withAlphaComponent(CGFloat(alpha)).cgColor
        }

        self.orderFront(nil)
    }

    func showProcessing() {
        recordingTimer?.invalidate()
        pulseTimer?.invalidate()
        dotView.layer?.backgroundColor = NSColor.systemOrange.cgColor
        waveView.reset()
    }

    func showSuccess() {
        dotView.layer?.backgroundColor = NSColor.systemGreen.cgColor
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.orderOut(nil)
        }
    }

    func showError() {
        dotView.layer?.backgroundColor = NSColor.systemRed.cgColor
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.orderOut(nil)
        }
    }

    func hideIndicator() {
        recordingTimer?.invalidate()
        pulseTimer?.invalidate()
        self.orderOut(nil)
    }

    func updateLevel(_ level: Float) {
        waveView.addLevel(level)
    }
}

// NSBezierPath → CGPath extension
extension NSBezierPath {
    var cgPath: CGPath {
        let path = CGMutablePath()
        var points = [CGPoint](repeating: .zero, count: 3)
        for i in 0..<elementCount {
            let type = element(at: i, associatedPoints: &points)
            switch type {
            case .moveTo: path.move(to: points[0])
            case .lineTo: path.addLine(to: points[0])
            case .curveTo: path.addCurve(to: points[2], control1: points[0], control2: points[1])
            case .closePath: path.closeSubpath()
            case .cubicCurveTo: path.addCurve(to: points[2], control1: points[0], control2: points[1])
            case .quadraticCurveTo: path.addQuadCurve(to: points[1], control: points[0])
            @unknown default: break
            }
        }
        return path
    }
}

// =============================================================================
// App Delegate
// =============================================================================
class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var indicator: IndicatorWindow!
    var recorder = AudioRecorder()
    var apiClient: OpenAIClient!
    var isRecording = false
    var recordingStartApp: NSRunningApplication?
    var levelTimer: Timer?

    // Option double-tap
    var optPressTime: TimeInterval = 0
    var optLastRelease: TimeInterval = 0
    var optOtherKey = false
    var optHeld = false

    // Ctrl double-tap
    var ctrlPressTime: TimeInterval = 0
    var ctrlLastRelease: TimeInterval = 0
    var ctrlOtherKey = false
    var ctrlHeld = false

    // Combo
    var comboTriggered = false

    var eventTap: CFMachPort?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let apiKey = loadApiKey()
        guard !apiKey.isEmpty else {
            let alert = NSAlert()
            alert.messageText = "API Key Bulunamadi"
            alert.informativeText = ".env dosyasina OPENAI_API_KEY ekleyin."
            alert.runModal()
            NSApp.terminate(nil)
            return
        }
        apiClient = OpenAIClient(apiKey: apiKey)

        // Menu bar
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let img = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Voice to Text") {
            img.isTemplate = true
            statusItem.button?.image = img
        } else {
            statusItem.button?.title = "V"
        }
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Hakkinda", action: #selector(showAbout), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Cikis", action: #selector(quitApp), keyEquivalent: "q"))
        statusItem.menu = menu

        // Indicator
        indicator = IndicatorWindow()
        indicator.onCancel = { [weak self] in self?.cancelRecording() }

        // Keyboard monitor
        setupEventTap()

        print("Voice to Text v3.0 — Hazir")
    }

    func setupEventTap() {
        let mask: CGEventMask = (1 << CGEventType.flagsChanged.rawValue)
            | (1 << CGEventType.keyDown.rawValue)

        let callback: CGEventTapCallBack = { proxy, type, event, refcon in
            let appDelegate = Unmanaged<AppDelegate>.fromOpaque(refcon!).takeUnretainedValue()
            appDelegate.handleEvent(type: type, event: event)
            return Unmanaged.passRetained(event)
        }

        let refcon = Unmanaged.passUnretained(self).toOpaque()
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: callback,
            userInfo: refcon
        ) else {
            print("HATA: CGEventTap olusturulamadi! Accessibility izni gerekli.")
            return
        }

        eventTap = tap
        let source = CFMachPortCreateRunLoopSource(nil, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
        print("Keyboard monitor aktif")
    }

    func handleEvent(type: CGEventType, event: CGEvent) {
        let now = ProcessInfo.processInfo.systemUptime
        let keycode = event.getIntegerValueField(.keyboardEventKeycode)

        if type == .keyDown {
            if keycode == 53 { // ESC
                DispatchQueue.main.async { [weak self] in
                    if self?.isRecording == true {
                        self?.cancelRecording()
                    }
                }
            } else {
                optOtherKey = true
                ctrlOtherKey = true
            }
            return
        }

        guard type == .flagsChanged else { return }

        let flags = event.flags

        let optDown = flags.contains(.maskAlternate)
        let ctrlDown = flags.contains(.maskControl)

        // Option pressed
        if optDown && !optHeld {
            optHeld = true
            optPressTime = now
            optOtherKey = false
            if ctrlHeld && !comboTriggered {
                comboTriggered = true
                DispatchQueue.global().async { [weak self] in self?.handleEnglishTranslation() }
            }
        }
        // Option released
        if !optDown && optHeld {
            optHeld = false
            if comboTriggered { if !ctrlHeld { comboTriggered = false }; optLastRelease = 0; return }

            let holdMs = (now - optPressTime) * 1000
            if holdMs > kMaxTapHoldMs || optOtherKey { optLastRelease = 0; return }

            let gapMs = (now - optLastRelease) * 1000
            if optLastRelease > 0 && gapMs < kDoubleTapIntervalMs {
                optLastRelease = 0
                DispatchQueue.main.async { [weak self] in self?.toggleRecording() }
            } else {
                optLastRelease = now
            }
        }

        // Ctrl pressed
        if ctrlDown && !ctrlHeld {
            ctrlHeld = true
            ctrlPressTime = now
            ctrlOtherKey = false
            if optHeld && !comboTriggered {
                comboTriggered = true
                DispatchQueue.global().async { [weak self] in self?.handleEnglishTranslation() }
            }
        }
        // Ctrl released
        if !ctrlDown && ctrlHeld {
            ctrlHeld = false
            if comboTriggered { if !optHeld { comboTriggered = false }; ctrlLastRelease = 0; return }

            let holdMs = (now - ctrlPressTime) * 1000
            if holdMs > kMaxTapHoldMs || ctrlOtherKey { ctrlLastRelease = 0; return }

            let gapMs = (now - ctrlLastRelease) * 1000
            if ctrlLastRelease > 0 && gapMs < kDoubleTapIntervalMs {
                ctrlLastRelease = 0
                DispatchQueue.global().async { [weak self] in self?.handleTranslation() }
            } else {
                ctrlLastRelease = now
            }
        }
    }

    // MARK: - Recording
    func toggleRecording() {
        if isRecording { stopRecording() } else { startRecording() }
    }

    func startRecording() {
        recordingStartApp = NSWorkspace.shared.frontmostApplication
        isRecording = true
        recorder.startRecording()
        indicator.showRecording()

        levelTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.indicator.updateLevel(self.recorder.currentLevel)
        }
    }

    func stopRecording() {
        isRecording = false
        levelTimer?.invalidate()
        indicator.showProcessing()

        guard let url = recorder.stopRecording() else {
            indicator.hideIndicator()
            return
        }

        let targetLang = indicator.selectedLang
        let startApp = recordingStartApp

        apiClient.transcribe(fileURL: url) { [weak self] text in
            guard let self = self, let text = text, !text.isEmpty else {
                DispatchQueue.main.async { self?.indicator.showError() }
                return
            }

            self.apiClient.translate(text: text, to: targetLang) { translated in
                let result = translated ?? text
                DispatchQueue.main.async {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(result, forType: .string)

                    // Orijinal uygulamaya don
                    if let app = startApp {
                        app.activate()
                        usleep(300_000)
                    }

                    // Cmd+V
                    self.simulatePaste()
                    self.indicator.showSuccess()
                }
            }
        }
    }

    func cancelRecording() {
        isRecording = false
        levelTimer?.invalidate()
        recorder.cleanup()
        indicator.hideIndicator()
    }

    // MARK: - Translation
    func handleTranslation() {
        let oldClip = NSPasteboard.general.string(forType: .string) ?? ""
        simulateCopy()
        usleep(200_000)
        let newClip = NSPasteboard.general.string(forType: .string) ?? ""
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(oldClip, forType: .string)

        guard !newClip.isEmpty, newClip != oldClip else { return }

        apiClient.translate(text: newClip, to: "tr") { [weak self] result in
            guard let result = result else { return }
            DispatchQueue.main.async {
                // Popup goster
                self?.showTranslationPopup(result)
            }
        }
    }

    func handleEnglishTranslation() {
        simulateSelectAll()
        usleep(150_000)
        simulateCopy()
        usleep(200_000)

        guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else { return }

        apiClient.translate(text: text, to: "en") { result in
            guard let result = result else { return }
            DispatchQueue.main.async {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(result, forType: .string)
                usleep(100_000)
                self.simulateSelectAll()
                usleep(150_000)
                self.simulatePaste()
            }
        }
    }

    func showTranslationPopup(_ text: String) {
        let popup = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 300, height: 80),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        popup.isOpaque = false
        popup.backgroundColor = .clear
        popup.level = .floating
        popup.hasShadow = true

        let bg = NSVisualEffectView(frame: popup.contentView!.bounds)
        bg.material = .hudWindow
        bg.state = .active
        bg.wantsLayer = true
        bg.layer?.cornerRadius = 12
        popup.contentView?.addSubview(bg)

        let label = NSTextField(wrappingLabelWithString: text)
        label.font = NSFont.systemFont(ofSize: 13)
        label.textColor = .white
        label.frame = bg.bounds.insetBy(dx: 14, dy: 10)
        bg.addSubview(label)

        // Popup boyutunu metne gore ayarla
        let size = label.sizeThatFits(NSSize(width: 280, height: 400))
        let w = min(size.width + 28, 360)
        let h = min(size.height + 20, 300)

        let mouse = NSEvent.mouseLocation
        popup.setFrame(NSRect(x: mouse.x + 16, y: mouse.y - h - 16, width: w, height: h), display: true)
        bg.frame = popup.contentView!.bounds
        label.frame = bg.bounds.insetBy(dx: 14, dy: 10)

        popup.orderFront(nil)

        // 5 saniye sonra kapat veya ESC ile
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            popup.orderOut(nil)
        }
    }

    // MARK: - Key simulation
    func simulatePaste() {
        let src = CGEventSource(stateID: .hidSystemState)
        let vDown = CGEvent(keyboardEventSource: src, virtualKey: 0x09, keyDown: true) // V key
        let vUp = CGEvent(keyboardEventSource: src, virtualKey: 0x09, keyDown: false)
        vDown?.flags = .maskCommand
        vUp?.flags = .maskCommand
        vDown?.post(tap: .cghidEventTap)
        vUp?.post(tap: .cghidEventTap)
    }

    func simulateCopy() {
        let src = CGEventSource(stateID: .hidSystemState)
        let cDown = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: true) // C key
        let cUp = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: false)
        cDown?.flags = .maskCommand
        cUp?.flags = .maskCommand
        cDown?.post(tap: .cghidEventTap)
        cUp?.post(tap: .cghidEventTap)
    }

    func simulateSelectAll() {
        let src = CGEventSource(stateID: .hidSystemState)
        let aDown = CGEvent(keyboardEventSource: src, virtualKey: 0x00, keyDown: true) // A key
        let aUp = CGEvent(keyboardEventSource: src, virtualKey: 0x00, keyDown: false)
        aDown?.flags = .maskCommand
        aUp?.flags = .maskCommand
        aDown?.post(tap: .cghidEventTap)
        aUp?.post(tap: .cghidEventTap)
    }

    @objc func showAbout() {
        let alert = NSAlert()
        alert.messageText = "Voice to Text Mac v3.0"
        alert.informativeText = "Option x2 → Kayit baslat/durdur\nTR/RU/EN → Hedef dil sec\nCtrl x2 → Turkce ceviri\nCtrl+Option → Ingilizce ceviri\nESC → Iptal"
        alert.runModal()
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }
}

// =============================================================================
// Main
// =============================================================================
// Single instance kontrolu
let runningApps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.ishflow.voice-to-text")
if runningApps.count > 1 {
    print("Zaten calisiyor.")
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory) // Dock'ta gozukmesin
let delegate = AppDelegate()
app.delegate = delegate
app.run()
