const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const native_module = b.addModule("harness-native", .{
        .root_source_file = b.path("src/harness/native/root.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });

    const lib = b.addLibrary(.{
        .linkage = .static,
        .name = "harness-native",
        .root_module = native_module,
    });
    b.installArtifact(lib);

    // Shared-library artifact for the stable C ABI consumed by the Python
    // loader (src/harness/_native.py) from zig-out/lib.
    const dylib = b.addLibrary(.{
        .linkage = .dynamic,
        .name = "harness_native",
        .root_module = native_module,
    });
    b.installArtifact(dylib);

    const unit_tests = b.addTest(.{
        .root_module = native_module,
    });
    const run_unit_tests = b.addRunArtifact(unit_tests);

    const test_step = b.step("test", "Run the native core smoke tests");
    test_step.dependOn(&run_unit_tests.step);
}
