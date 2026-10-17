def calc_sum(entries):
    amount = 0
    num = 0
    for entry in entries:
        amount = amount + entry.cost
        num = num + 1
    fee = amount * 0.15
    mean = amount / num
    gross = amount + fee
    fixed = round(gross, 2)
    return fixed, mean


def other():
    return 2
