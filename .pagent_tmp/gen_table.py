# -*- coding: utf-8 -*-
import unicodedata

def w(s):
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)

def pad(s, n, a='c'):
    t = n - w(s)
    if t < 0:
        return s
    if a == 'l':
        return s + ' ' * t
    if a == 'r':
        return ' ' * t + s
    l = t // 2
    return ' ' * l + s + ' ' * (t - l)

W = [8, 8, 9, 8, 8, 8, 8]
A = ['c', 'r', 'r', 'r', 'r', 'r', 'r']
g1 = W[1] + W[2] + W[3] + 2
g2 = W[4] + W[5] + W[6] + 2

def line(l, m, r, segs):
    return l + m.join('─' * s for s in segs) + r

def row(c):
    return '│' + '│'.join(pad(c[i], W[i], A[i]) for i in range(len(W))) + '│'

segs7 = [W[0]] + W[1:]
print(line('┌', '┬', '┐', [W[0], g1, g2]))
print('│' + pad('', W[0]) + '│' + pad('上半年', g1) + '│' + pad('下半年', g2) + '│')
print('│' + ' ' * W[0] + '├' + '┬'.join('─' * x for x in W[1:]) + '┤')
print(row(['区域', '目标', '实际', '完成率', '目标', '实际', '完成率']))
print(line('├', '┼', '┤', segs7))

data = [('华东', 1200, 1260, 980, 1035),
        ('华北', 980, 910, 1020, 1080),
        ('华南', 1500, 1620, 1600, 1710),
        ('西部', 620, 540, 700, 655)]
for n, a1, b1, a2, b2 in data:
    print(row([n, str(a1), str(b1), '%.1f%%' % (b1 / a1 * 100),
               str(a2), str(b2), '%.1f%%' % (b2 / a2 * 100)]))
print(line('├', '┼', '┤', segs7))

t1 = sum(d[1] for d in data); t2 = sum(d[2] for d in data)
t3 = sum(d[3] for d in data); t4 = sum(d[4] for d in data)
print(row(['合计', str(t1), str(t2), '%.1f%%' % (t2 / t1 * 100),
           str(t3), str(t4), '%.1f%%' % (t4 / t3 * 100)]))
print(line('└', '┴', '┘', segs7))
