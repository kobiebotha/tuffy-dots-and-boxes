import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { BoardView } from "./Board";

describe("badge and dashboard board access", () => {
  it("keeps the ordinary dashboard board read-only", () => {
    const board = renderToStaticMarkup(<BoardView cols={5} rows={4} moves={[]} />);
    expect(board).toContain('role="img"');
    expect(board).not.toContain('role="button"');
    expect(board).not.toContain('tabindex=');
  });

  it("offers one named keyboard target per free edge on the emulator", () => {
    const board = renderToStaticMarkup(<BoardView cols={5} rows={4} moves={[{ orientation: "h", row: 0, col: 0 }]} onEdge={() => {}} />);
    expect(board).toContain('role="group"');
    expect(board.match(/role="button"/g)).toHaveLength(48);
    expect(board).not.toContain('aria-label="Claim horizontal edge row 1 column 1"');
    expect(board).toContain('aria-label="Claim vertical edge row 4 column 6"');
    expect(board.match(/tabindex="0"/g)).toHaveLength(48);
    // Real rectangular hit areas retain a nonzero bounding box for pointer
    // controls, including horizontal and vertical SVG edges.
    expect(board).toContain('width="72" height="28" fill="transparent" role="button"');
    expect(board).toContain('width="28" height="72" fill="transparent" role="button"');
  });

  it("disables every edge while it is the opponent's turn or a move is pending", () => {
    const board = renderToStaticMarkup(<BoardView cols={5} rows={4} moves={[]} onEdge={() => {}} disabled />);
    expect(board.match(/aria-disabled="true"/g)).toHaveLength(49);
    expect(board).not.toContain('tabindex="0"');
  });
});
