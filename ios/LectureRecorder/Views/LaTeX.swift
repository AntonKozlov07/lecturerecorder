import Foundation

/// Turns the LaTeX that Claude writes for formulas into readable Unicode text, e.g.
/// `S = k_B \ln W` → `S = k_B ln W`, `\frac{1}{2}mv^2` → `1/2 mv²`, `\Delta S \geq 0` → `ΔS ≥ 0`.
/// The phone has no math renderer, and this covers what lecture notes typically contain.
enum LaTeX {
    /// Replaces every `$...$` and `$$...$$` in `text` with plain Unicode.
    /// With `markdown`, display math becomes its own paragraph and Markdown characters are escaped.
    static func render(in text: String, markdown: Bool = false) -> String {
        var s = replace(text, #"\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]"#) { g in
            let body = plain(g[1].isEmpty ? g[2] : g[1])
            return markdown ? "\n\n" + escape(body) + "\n\n" : body
        }
        s = replace(s, #"(?<![\\$\w])\$(?=\S)([^\n$]+?)(?<=\S)\$(?![\d\w])|\\\((.+?)\\\)"#) { g in
            let body = plain(g[1].isEmpty ? g[2] : g[1])
            return markdown ? escape(body) : body
        }
        return s
    }

    /// Converts one LaTeX expression (without the dollar signs) to Unicode text.
    static func plain(_ input: String) -> String {
        var s = input
        s = s.replacingOccurrences(of: #"\{"#, with: "\u{E000}").replacingOccurrences(of: #"\}"#, with: "\u{E001}")
        s = replace(s, #"\\[,;:! ]|\\\\|~"#) { _ in " " }
        s = replaceLoop(s, #"\\(?:text|mathrm|mathbf|mathit|mathsf|operatorname|mathcal|mathbb|boldsymbol|vec|hat|bar|overline|tilde)\s*\{([^{}]*)\}"#) { g in g[1] }
        s = replaceLoop(s, #"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}"#) { g in group(g[1]) + "/" + group(g[2]) + " " }
        s = replaceLoop(s, #"\\sqrt\s*\{([^{}]*)\}"#) { g in "√" + group(g[1]) }
        s = replace(s, #"\\([A-Za-z]+)"#) { g in symbols[g[1]] ?? g[1] }
        s = replace(s, #"\^\{([^{}]*)\}|\^(\S)"#) { g in script(g[1].isEmpty ? g[2] : g[1], superscripts, "^") }
        s = replace(s, #"_\{([^{}]*)\}|_(\S)"#) { g in script(g[1].isEmpty ? g[2] : g[1], subscripts, "_") }
        s = s.replacingOccurrences(of: "{", with: "").replacingOccurrences(of: "}", with: "")
        s = s.replacingOccurrences(of: "\u{E000}", with: "{").replacingOccurrences(of: "\u{E001}", with: "}")
        s = s.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        s = s.replacingOccurrences(of: #" ([,.;)])"#, with: "$1", options: .regularExpression)
        return s.trimmingCharacters(in: .whitespaces)
    }

    private static func group(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespaces)
        return t.range(of: #"^[\p{L}\p{N}.]+$"#, options: .regularExpression) != nil ? t : "(" + t + ")"
    }

    private static func script(_ text: String, _ map: [Character: Character], _ mark: String) -> String {
        let t = text.trimmingCharacters(in: .whitespaces)
        if !t.isEmpty, t.allSatisfy({ map[$0] != nil }) { return String(t.compactMap { map[$0] }) }
        return mark + (t.count == 1 ? t : "(" + t + ")")
    }

    private static func escape(_ s: String) -> String {
        var out = ""
        for c in s {
            if "*_`[]<>#".contains(c) { out.append("\\") }
            out.append(c)
        }
        return out
    }

    private static func table(_ from: String, _ to: String) -> [Character: Character] {
        Dictionary(uniqueKeysWithValues: zip(from, to))
    }

    private static let superscripts = table("0123456789+-−=()niaxyk°′*T", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱᵃˣʸᵏ°′*ᵀ")
    private static let subscripts = table("0123456789+-−=()aeoxhklmnpstijruv", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥ")

    private static let symbols: [String: String] = [
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
        "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
        "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
        "chi": "χ", "psi": "ψ", "omega": "ω",
        "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
        "Psi": "Ψ", "Omega": "Ω",
        "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗",
        "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡",
        "sim": "∼", "simeq": "≃", "propto": "∝", "ll": "≪", "gg": "≫",
        "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "Σ", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮",
        "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
        "leftrightarrow": "↔", "Leftrightarrow": "⇔", "implies": "⇒", "iff": "⇔", "mapsto": "↦",
        "cdots": "⋯", "ldots": "…", "dots": "…", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
        "cup": "∪", "cap": "∩", "emptyset": "∅", "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧", "lor": "∨",
        "circ": "°", "degree": "°", "prime": "′", "hbar": "ℏ", "ell": "ℓ", "angle": "∠", "perp": "⊥",
        "parallel": "∥", "langle": "⟨", "rangle": "⟩", "quad": " ", "qquad": " ",
        "left": "", "right": "", "big": "", "Big": "", "bigg": "", "Bigg": "", "displaystyle": "", "limits": "",
    ]

    private static func replace(_ s: String, _ pattern: String, _ f: ([String]) -> String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return s }
        let ns = s as NSString
        var out = ""
        var last = 0
        for m in re.matches(in: s, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let groups = (0..<m.numberOfRanges).map { i -> String in
                let r = m.range(at: i)
                return r.location == NSNotFound ? "" : ns.substring(with: r)
            }
            out += f(groups)
            last = m.range.location + m.range.length
        }
        return out + ns.substring(from: last)
    }

    private static func replaceLoop(_ s: String, _ pattern: String, _ f: ([String]) -> String) -> String {
        var current = s
        for _ in 0..<8 {
            let next = replace(current, pattern, f)
            if next == current { break }
            current = next
        }
        return current
    }
}
