// generated by py2many --dlang=1
import std;
import std.algorithm : equal;
import std.range : iota;
import std.typecons : tuple;

int[] bubble_sort(int[] seq) {
  auto L = seq.length;
  foreach (_; iota(0, L, 1)) {
    foreach (n; iota(1, L, 1)) {

      if (seq[n] < seq[(n - 1)]) {
        auto __tmp1 = tuple(seq[n], seq[(n - 1)]);
        seq[(n - 1)] = __tmp1[0];
        seq[n] = __tmp1[1];
      }
    }
  }
  return seq;
}

void main(string[] argv) {
  int[] unsorted = [14, 11, 19, 5, 16, 10, 19, 12, 5, 12];
  int[] expected = [5, 5, 10, 11, 12, 12, 14, 16, 19, 19];
  assert(equal(bubble_sort(unsorted), expected));
  writeln(format("%s", "OK"));
}
