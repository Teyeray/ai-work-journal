// vision-ocr: local screenshot OCR with Apple's Vision framework.
//
// Reads one image (PNG or JPEG) from a file path given as argv[1] and writes
// one JSON object to stdout: {"text": "...", "lines": N, "engine": "macos-vision"}.
// It never touches the network and never writes anything except stdout.
//
// Recognition languages default to zh-Hans + zh-Hant + en-US so one pass reads
// a mixed Chinese/English screen; pass extra languages as argv[2..].
// Language correction is off on purpose: screens are full of identifiers,
// file paths and code, which a language model "corrects" into nonsense.

import Foundation
import Vision
import AppKit

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write("vision-ocr: \(message)\n".data(using: .utf8)!)
    exit(code)
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: vision-ocr <image-path> [languages...]") }
let path = args[1]
let languages = args.count > 2
    ? Array(args[2...])
    : ["zh-Hans", "zh-Hant", "en-US"]

guard let image = NSImage(contentsOfFile: path) else { fail("cannot read image: \(path)") }
var rect = NSRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    fail("cannot decode image: \(path)") }

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
// Vision rejects languages the running OS does not know; keep the ones it has.
let supported = (try? request.supportedRecognitionLanguages()) ?? []
if !supported.isEmpty {
    let keep = languages.filter { supported.contains($0) }
    if !keep.isEmpty { request.recognitionLanguages = keep }
}

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("vision failed: \(error.localizedDescription)")
}

let observations = request.results ?? []
// Vision hands back observations roughly top-to-bottom, but order within a row
// is unstable; Vision's y axis points up, so midY sorts descending.
let sorted = observations.sorted { a, b in
    if abs(a.boundingBox.midY - b.boundingBox.midY) > 0.01 { return a.boundingBox.midY > b.boundingBox.midY }
    return a.boundingBox.minX < b.boundingBox.minX
}
let lines = sorted.compactMap { $0.topCandidates(1).first?.string }
let text = lines.joined(separator: "\n")

let out: [String: Any] = ["lines": lines.count, "engine": "macos-vision", "text": text]
guard let data = try? JSONSerialization.data(withJSONObject: out, options: [.withoutEscapingSlashes]),
      let encoded = String(data: data, encoding: .utf8) else {
    fail("cannot encode result")
}
print(encoded)