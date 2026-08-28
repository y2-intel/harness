const std = @import("std");

pub const url = "https://github.com/y2-intel/harness/issues/new";

test "feedback URL stays on the Y2 harness repository" {
    try std.testing.expectEqualStrings("https://github.com/y2-intel/harness/issues/new", url);
}
