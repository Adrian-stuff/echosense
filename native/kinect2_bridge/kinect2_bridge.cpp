// echosense-kinect2: streams Kinect v2 depth frames from libfreenect2 to stdout.
//
// stdout protocol (read by echosense/sensors/kinect2.py):
//   line 1 : {"type":"hello",...intrinsics...}\n    or {"type":"error","message":"..."}\n then exit(2)
//   frames : "ESD2" | u32 seq | f64 t (steady clock, s) | u32 width | u32 height | float32[w*h] depth (mm)
// Everything else (libfreenect2 logs, warnings) goes to stderr.
//
//   echosense-kinect2 [--pipeline auto|cl|gl|cpu] [--min-depth 0.5] [--max-depth 4.5] [--serial S] [--fake]
//
// --fake emits synthetic frames without hardware (and is all you get when built without libfreenect2),
// so the bridge protocol can be tested anywhere, including the Docker virtual device.

#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#ifdef ECHOSENSE_WITH_FREENECT2
#include <libfreenect2/frame_listener_impl.h>
#include <libfreenect2/libfreenect2.hpp>
#include <libfreenect2/logger.h>
#include <libfreenect2/packet_pipeline.h>
#endif

static std::atomic<bool> g_run{true};
static void on_signal(int) { g_run = false; }

static double now_s() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

static bool write_all(const void* data, size_t n) {
  const char* p = static_cast<const char*>(data);
  while (n) {
    size_t w = fwrite(p, 1, n, stdout);
    if (w == 0) return false;
    p += w;
    n -= w;
  }
  return true;
}

static bool write_frame(uint32_t seq, double t, uint32_t w, uint32_t h, const float* mm) {
  char hdr[24];
  memcpy(hdr, "ESD2", 4);
  memcpy(hdr + 4, &seq, 4);
  memcpy(hdr + 8, &t, 8);
  memcpy(hdr + 16, &w, 4);
  memcpy(hdr + 20, &h, 4);
  if (!write_all(hdr, sizeof hdr) || !write_all(mm, size_t(w) * h * 4)) return false;
  return fflush(stdout) == 0;
}

static void hello(const std::string& serial, const std::string& pipeline, float fx, float fy, float cx, float cy) {
  printf("{\"type\":\"hello\",\"serial\":\"%s\",\"pipeline\":\"%s\",\"width\":512,\"height\":424,"
         "\"fx\":%.4f,\"fy\":%.4f,\"cx\":%.4f,\"cy\":%.4f}\n",
         serial.c_str(), pipeline.c_str(), fx, fy, cx, cy);
  fflush(stdout);
}

static int fail(const std::string& msg, int code = 2) {
  printf("{\"type\":\"error\",\"message\":\"%s\"}\n", msg.c_str());
  fflush(stdout);
  std::cerr << "echosense-kinect2: " << msg << std::endl;
  return code;
}

// Synthetic scene: floor seen from a chest-height sensor tilted down 10 deg, plus a box that moves.
static int run_fake(float min_d, float max_d) {
  const int W = 512, H = 424;
  const float fx = 365.456f, fy = 365.456f, cx = 254.878f, cy = 205.395f;
  hello("FAKE0000", "fake", fx, fy, cx, cy);
  std::vector<float> mm(W * H);
  const float h = 1.25f, pitch = -10.f * 3.14159265f / 180.f;
  uint32_t seq = 0;
  auto next = std::chrono::steady_clock::now();
  while (g_run) {
    double t = now_s();
    float box_z = 2.0f + 1.2f * std::sin(t * 0.7);
    for (int v = 0; v < H; ++v) {
      for (int u = 0; u < W; ++u) {
        float xr = (u - cx) / fx, yr = (v - cy) / fy;  // camera ray, z = 1
        // camera -> body: y_up = -cos(p) * y - sin(p) * z ... (pitch about x)
        float dy = -std::cos(-pitch) * yr - std::sin(-pitch);
        float dz = -std::sin(-pitch) * yr + std::cos(-pitch);
        float depth = 0.f;
        if (dy < -1e-4f) depth = -h / dy;  // floor
        // box: |x| < 0.3, 0 < y < 1.0, z in [box_z, box_z + 0.4] (body frame)
        float tz = box_z / dz;
        if (tz > 0) {
          float x = xr * tz, y = h + dy * tz;
          if (std::fabs(x) < 0.3f && y > 0.f && y < 1.0f && (depth == 0.f || tz < depth)) depth = tz;
        }
        mm[v * W + u] = (depth >= min_d && depth <= max_d) ? depth * 1000.f : 0.f;
      }
    }
    if (!write_frame(++seq, t, W, H, mm.data())) break;
    next += std::chrono::milliseconds(33);
    std::this_thread::sleep_until(next);
  }
  return 0;
}

#ifdef ECHOSENSE_WITH_FREENECT2
class StderrLogger : public libfreenect2::Logger {
 public:
  explicit StderrLogger(Level l) { level_ = l; }
  void log(Level level, const std::string& message) override {
    if (level > level_) return;
    std::cerr << "[libfreenect2 " << Logger::level2str(level) << "] " << message << std::endl;
  }
};

static libfreenect2::PacketPipeline* make_pipeline(const std::string& want, std::string& got) {
#ifdef LIBFREENECT2_WITH_OPENCL_SUPPORT
  if (want == "cl" || want == "auto") { got = "opencl"; return new libfreenect2::OpenCLPacketPipeline(); }
#endif
#ifdef LIBFREENECT2_WITH_OPENGL_SUPPORT
  if (want == "gl" || want == "auto") { got = "opengl"; return new libfreenect2::OpenGLPacketPipeline(); }
#endif
  got = "cpu";
  return new libfreenect2::CpuPacketPipeline();
}

static int run_device(const std::string& pipe_name, const std::string& serial_arg, float min_d, float max_d) {
  libfreenect2::setGlobalLogger(new StderrLogger(libfreenect2::Logger::Warning));  // never log to stdout
  libfreenect2::Freenect2 fn;
  if (fn.enumerateDevices() == 0) return fail("No Kinect v2 found. Check USB 3.0 cable, the 12 V adapter and udev rules.");
  std::string serial = serial_arg.empty() ? fn.getDefaultDeviceSerialNumber() : serial_arg;
  std::string got;
  libfreenect2::PacketPipeline* pipeline = make_pipeline(pipe_name, got);
  libfreenect2::Freenect2Device* dev = fn.openDevice(serial, pipeline);
  if (!dev && got != "cpu") {  // GPU pipeline unavailable at runtime: fall back
    got = "cpu";
    dev = fn.openDevice(serial, new libfreenect2::CpuPacketPipeline());
  }
  if (!dev) return fail("Kinect v2 found but could not be opened (permissions? see udev rules in README)");
  libfreenect2::SyncMultiFrameListener listener(libfreenect2::Frame::Depth);
  dev->setIrAndDepthFrameListener(&listener);
  libfreenect2::Freenect2Device::Config cfg;
  cfg.MinDepth = min_d;
  cfg.MaxDepth = max_d;
  cfg.EnableBilateralFilter = true;
  cfg.EnableEdgeAwareFilter = true;  // removes most flying pixels
  dev->setConfiguration(cfg);
  if (!dev->startStreams(false, true)) {
    dev->close();
    return fail("Kinect v2 did not start streaming");
  }
  auto ir = dev->getIrCameraParams();
  hello(serial, got, ir.fx, ir.fy, ir.cx, ir.cy);
  libfreenect2::FrameMap frames;
  uint32_t seq = 0;
  int timeouts = 0;
  while (g_run) {
    if (!listener.waitForNewFrame(frames, 2000)) {
      std::cerr << "echosense-kinect2: no frame for 2 s" << std::endl;
      if (++timeouts >= 3) break;  // let the Python side raise the fail-safe alarm
      continue;
    }
    timeouts = 0;
    libfreenect2::Frame* d = frames[libfreenect2::Frame::Depth];
    bool ok = write_frame(++seq, now_s(), uint32_t(d->width), uint32_t(d->height), reinterpret_cast<const float*>(d->data));
    listener.release(frames);
    if (!ok) break;  // reader went away
  }
  dev->stop();
  dev->close();
  return 0;
}
#endif

int main(int argc, char** argv) {
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);
  std::signal(SIGPIPE, on_signal);
  std::string pipeline = "auto", serial;
  float min_d = 0.5f, max_d = 4.5f;
  bool fake = false;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto val = [&](void) -> std::string { return i + 1 < argc ? argv[++i] : ""; };
    if (a == "--pipeline") pipeline = val();
    else if (a == "--serial") serial = val();
    else if (a == "--min-depth") min_d = std::stof(val());
    else if (a == "--max-depth") max_d = std::stof(val());
    else if (a == "--fake") fake = true;
    else if (a == "--help" || a == "-h") {
      std::cerr << "usage: echosense-kinect2 [--pipeline auto|cl|gl|cpu] [--min-depth M] [--max-depth M] [--serial S] [--fake]\n";
      return 0;
    }
  }
  if (fake) return run_fake(min_d, max_d);
#ifdef ECHOSENSE_WITH_FREENECT2
  return run_device(pipeline, serial, min_d, max_d);
#else
  return fail("built without libfreenect2; only --fake is available");
#endif
}
