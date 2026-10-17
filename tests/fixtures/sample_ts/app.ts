import { helper, usesInternal } from "./util";
import { unusedImport } from "./other";
import * as path from "path";

export function main(): void {
  console.log(helper("world"), usesInternal());
  const s = new Service();
  s.serve();
  path.join("a", "b");
  checkVars();
}

class Service {
  serve(): void {
    this.helper2();
  }
  private helper2(): void {}
  deadMethod(): void {}
}

function deadTopLevel(): void {}

const deadArrow = (): number => 1;
const usedArrow = (): number => 2;

class DeadClass {
  run(): void {}
}

const unusedVar = 1;
const usedVar = 2;

function checkVars(): number {
  return usedVar + usedArrow();
}

@sealed
class Decorated {
  x = 1;
}

export default main;
