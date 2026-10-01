import XCTest
@testable import LectureRecorder

@MainActor
final class LaTeXTests: XCTestCase {
    func testFormulasBecomeReadableText() {
        XCTAssertEqual(LaTeX.plain(#"S = k_B \ln W"#), "S = k_B ln W")
        XCTAssertEqual(LaTeX.plain(#"\frac{1}{2}mv^2"#), "1/2 mv²")
        XCTAssertEqual(LaTeX.plain(#"\Delta S \geq 0"#), "Δ S ≥ 0")
        XCTAssertEqual(LaTeX.plain(#"x_{12}^{-1}"#), "x₁₂⁻¹")
        XCTAssertEqual(LaTeX.plain(#"\sqrt{a^2 + b^2}"#), "√(a² + b²)")
    }

    func testDollarSignsInText() {
        XCTAssertEqual(LaTeX.render(in: "Energy $E = mc^2$ here, costs $5 and $6."), "Energy E = mc² here, costs $5 and $6.")
        XCTAssertTrue(MarkdownText.prepare("Formula: $$S = k_B \\ln W$$", links: false).contains("S = k\\_B ln W"))
    }
}
