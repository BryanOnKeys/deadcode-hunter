package com.example;

public class App {
    public static void main(String[] args) throws Exception {
        Service s = new Service();
        s.serve();
        // Only reachable through a string literal: the scanner cannot see
        // this link statically, so reflectiveOp must be downgraded, not high.
        Service.class.getMethod("reflectiveOp").invoke(s);
    }
}
