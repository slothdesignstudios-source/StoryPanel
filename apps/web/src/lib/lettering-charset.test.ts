import { describe, expect, it } from "vitest";
import { unsupportedLetteringChars, unsupportedLetteringMessage } from "./lettering-charset";

describe("lettering character coverage", () => {
  it("accepts Western European text and typographic punctuation", () => {
    expect(unsupportedLetteringChars("¿Qué? ¡Sí! Café, naïve façade — «Ærø» … “déjà vu” ½ £5 €3 Œuvre")).toEqual([]);
  });
  it("names each unsupported character once, in order, without changing the text", () => {
    const text = "Łódź, Привет 🙂 Łódź";
    expect(unsupportedLetteringChars(text)).toEqual(["Ł", "ź", "П", "р", "и", "в", "е", "т", "🙂"]);
    expect(text).toBe("Łódź, Привет 🙂 Łódź");
  });
  it("gives an actionable message that does not claim full Unicode support", () => {
    const m = unsupportedLetteringMessage(["Ł"]);
    expect(m).toContain("These characters can't be lettered yet: Ł.");
    expect(m).toContain("Latin-1");
    expect(m).toMatch(/Replace or remove them/);
    expect(m).not.toMatch(/unicode/i);
  });
});
