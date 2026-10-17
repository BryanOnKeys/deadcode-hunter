package com.example;

import java.util.ArrayList;
import static java.lang.Math.max;

public class Service implements Greeter {
    private int counter = 0;

    public void serve() {
        counter = max(counter, 1);
        helper("hi");
    }

    private void helper(String msg) {
        System.out.println(msg);
    }

    private void deadHelper() {
        counter = -1;
    }

    public void unusedPublic() {
    }

    public void reflectiveOp() {
    }

    @Override
    public void greet() {
    }
}
