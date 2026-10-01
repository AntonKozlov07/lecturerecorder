import XCTest

/// Walks through the app with sample data and saves a screenshot of each screen.
/// CI exports them from the test results so the design can be reviewed without an iPhone.
final class ScreenshotTests: XCTestCase {
    override func setUp() {
        continueAfterFailure = true
    }

    private func shot(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    @MainActor
    func testTour() throws {
        for dark in [false, true] {
            XCUIDevice.shared.appearance = dark ? .dark : .light
            let mode = dark ? "dark" : "light"
            let app = XCUIApplication()
            app.launchArguments = ["-uitest", "-sample"]
            app.launch()

            XCTAssertTrue(app.staticTexts["Week 3: Entropy"].waitForExistence(timeout: 10))
            shot(app, "\(mode)-01-lectures")

            app.staticTexts["Week 3: Entropy"].firstMatch.tap()
            XCTAssertTrue(app.staticTexts["Boltzmann wrote it as S equals k log W, and that's on his tombstone."].waitForExistence(timeout: 5))
            shot(app, "\(mode)-02-transcript")

            let sections = app.segmentedControls["sections"]
            sections.buttons.element(boundBy: 1).tap()
            shot(app, "\(mode)-03-notes")
            sections.buttons.element(boundBy: 2).tap()
            shot(app, "\(mode)-04-chat")
            sections.buttons.element(boundBy: 3).tap()
            shot(app, "\(mode)-05-quiz")
            if !dark {
                app.buttons["option-0-1"].firstMatch.tap()
                app.buttons["check-answers"].firstMatch.tap()
                shot(app, "\(mode)-06-quiz-graded")
            }
            sections.buttons.element(boundBy: 4).tap()
            shot(app, "\(mode)-07-cards")
            app.buttons["Show answer"].tap()
            sleep(1)
            shot(app, "\(mode)-08-card-answer")

            app.tabBars.buttons["Courses"].tap()
            XCTAssertTrue(app.staticTexts["PHYS 201"].firstMatch.waitForExistence(timeout: 5))
            shot(app, "\(mode)-09-courses")
            app.staticTexts["PHYS 201"].firstMatch.tap()
            shot(app, "\(mode)-10-course")

            app.tabBars.buttons["Settings"].tap()
            shot(app, "\(mode)-11-settings")

            app.tabBars.buttons["Lectures"].tap()
            app.navigationBars.buttons.element(boundBy: 0).tap()
            app.buttons["record"].tap()
            XCTAssertTrue(app.buttons["start"].waitForExistence(timeout: 5))
            shot(app, "\(mode)-12-new-recording")
            app.terminate()
        }
    }
}
