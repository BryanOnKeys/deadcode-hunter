export interface Shape {
  area(): number;
}

export type Alias = string;

export enum LiveEnum {
  A,
  B,
}

enum InternalDeadEnum {
  X,
  Y,
}

export class Circle implements Shape {
  area(): number {
    return 3.14;
  }
}

export function useEnum(): number {
  return LiveEnum.A;
}
