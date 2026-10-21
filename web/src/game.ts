export interface Move {
  orientation: "h" | "v";
  row: number;
  col: number;
  seat?: number;
}

export class Game {
  cols: number;
  rows: number;
  horizontal: number[];
  vertical: number[];
  boxes: number[];
  currentPlayer = 1;
  scores = [0, 0];
  moves = 0;

  constructor(cols = 5, rows = 4) {
    if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1) {
      throw new Error("Board dimensions must be positive integers");
    }
    this.cols = cols;
    this.rows = rows;
    this.horizontal = Array((cols * (rows + 1))).fill(0);
    this.vertical = Array(((cols + 1) * rows)).fill(0);
    this.boxes = Array(cols * rows).fill(0);
  }

  get finished(): boolean {
    return this.moves === this.horizontal.length + this.vertical.length;
  }

  get winner(): number | null {
    if (!this.finished) return null;
    if (this.scores[0] === this.scores[1]) return 0;
    return this.scores[0] > this.scores[1] ? 1 : 2;
  }

  edgeOwner(orientation: "h" | "v", row: number, col: number): number {
    const [edges, index] = this.edge(orientation, row, col);
    return edges[index];
  }

  claim(orientation: "h" | "v", row: number, col: number): Array<[number, number]> | null {
    const [edges, index] = this.edge(orientation, row, col);
    if (this.finished || edges[index]) return null;
    edges[index] = this.currentPlayer;
    this.moves += 1;
    const completed: Array<[number, number]> = [];
    if (orientation === "h") {
      if (row > 0 && this.completeBox(row - 1, col)) completed.push([row - 1, col]);
      if (row < this.rows && this.completeBox(row, col)) completed.push([row, col]);
    } else {
      if (col > 0 && this.completeBox(row, col - 1)) completed.push([row, col - 1]);
      if (col < this.cols && this.completeBox(row, col)) completed.push([row, col]);
    }
    if (completed.length) this.scores[this.currentPlayer - 1] += completed.length;
    else this.currentPlayer = 3 - this.currentPlayer;
    return completed;
  }

  private edge(orientation: "h" | "v", row: number, col: number): [number[], number] {
    if (!Number.isInteger(row) || !Number.isInteger(col)) throw new Error("Edge coordinates must be integers");
    if (orientation === "h" && row >= 0 && row <= this.rows && col >= 0 && col < this.cols) {
      return [this.horizontal, row * this.cols + col];
    }
    if (orientation === "v" && row >= 0 && row < this.rows && col >= 0 && col <= this.cols) {
      return [this.vertical, row * (this.cols + 1) + col];
    }
    if (orientation !== "h" && orientation !== "v") throw new Error("Edge orientation must be h or v");
    throw new Error("Edge is outside the board");
  }

  private completeBox(row: number, col: number): boolean {
    const index = row * this.cols + col;
    if (this.boxes[index]) return false;
    const horizontal = row * this.cols + col;
    const vertical = row * (this.cols + 1) + col;
    if (
      this.horizontal[horizontal] &&
      this.horizontal[horizontal + this.cols] &&
      this.vertical[vertical] &&
      this.vertical[vertical + 1]
    ) {
      this.boxes[index] = this.currentPlayer;
      return true;
    }
    return false;
  }
}

export function replay(cols: number, rows: number, moves: Move[]): Game {
  const game = new Game(cols, rows);
  for (const move of moves) {
    if (game.claim(move.orientation, move.row, move.col) === null) break;
  }
  return game;
}

export function groupCode(code: string): string {
  return code.split("").join(" ");
}
