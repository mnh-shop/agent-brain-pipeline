import fs from "node:fs";

export class Service extends BaseService {
  constructor(name: string) {
    this.name = name;
  }

  run(): string {
    return this.name;
  }
}

export function helper(value: number): number {
  return value + 1;
}

export const ANSWER = 42;

interface Thing {
  id: string;
}

enum Mode {
  A = "A",
  B = "B",
}
