import Foundation
import PDFKit
import UIKit
import UniformTypeIdentifiers

/// Turns files picked on the phone into course materials: PDFs (text read with PDFKit,
/// scans kept as the original), text files and photos.
enum MaterialImport {
    struct Result {
        var filename: String
        var kind: String
        var data: Data
        var text: String
        var pages: Int
    }

    enum ImportError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case .message(let m) = self { return m }; return nil }
    }

    static func read(url: URL) throws -> Result {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf: url)
        let name = url.lastPathComponent
        let type = UTType(filenameExtension: url.pathExtension.lowercased())
        if type?.conforms(to: .pdf) == true {
            guard let pdf = PDFDocument(data: data) else { throw ImportError.message("Couldn't read \(name).") }
            var pages: [String] = []
            for i in 0..<pdf.pageCount {
                if let text = pdf.page(at: i)?.string?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty {
                    pages.append("[Page \(i + 1)]\n\(text)")
                }
            }
            let text = pages.joined(separator: "\n\n")
            let scanned = text.count < 50 * max(1, pdf.pageCount)
            if scanned && data.count > 30 * 1024 * 1024 {
                throw ImportError.message("\(name) looks scanned and is over 30 MB. Split it into smaller files.")
            }
            return Result(filename: name, kind: scanned ? "pdf_scan" : "pdf", data: data, text: scanned ? "" : text,
                          pages: pdf.pageCount)
        }
        if type?.conforms(to: .image) == true, let image = UIImage(data: data) {
            return try photo(image, name: name)
        }
        if type?.conforms(to: .text) == true || ["md", "txt", "csv", "tex"].contains(url.pathExtension.lowercased()) {
            return Result(filename: name, kind: "text", data: data, text: String(decoding: data, as: UTF8.self), pages: 0)
        }
        throw ImportError.message("\(name): on iPhone you can add PDFs, text files and photos. Add Word or PowerPoint files on the PC.")
    }

    /// Photos are resized and compressed to stay under the API's 5 MB image limit.
    static func photo(_ image: UIImage, name: String) throws -> Result {
        var img = image
        let longest = max(image.size.width, image.size.height)
        if longest > 2400 {
            let scale = 2400 / longest
            let size = CGSize(width: image.size.width * scale, height: image.size.height * scale)
            img = UIGraphicsImageRenderer(size: size).image { _ in image.draw(in: CGRect(origin: .zero, size: size)) }
        }
        for quality in [0.8, 0.6, 0.4] {
            if let jpeg = img.jpegData(compressionQuality: quality), jpeg.count < 4_900_000 {
                let base = (name as NSString).deletingPathExtension
                return Result(filename: "\(base.isEmpty ? "Photo" : base).jpg", kind: "image", data: jpeg, text: "", pages: 1)
            }
        }
        throw ImportError.message("That photo is too large.")
    }
}
