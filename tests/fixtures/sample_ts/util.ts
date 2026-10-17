export function helper(name: string): string {
  return `hello ${name}`;
}

export function usesInternal(): string {
  return helper("x");
}

function internalDead(): number {
  return 42;
}

export const exportedConst = 1;
