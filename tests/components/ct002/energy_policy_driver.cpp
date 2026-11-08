// Small stdin/stdout bridge for shared Python/C++ policy vectors. No ESPHome,
// network or battery needed; compiled by test_energy_policy_parity.py.
#include "esphome/components/ct002/energy_policy.h"
#include <iomanip>
#include <iostream>

int main(int argc, char **) {
  using namespace esphome::ct002;
  std::cout << std::setprecision(17);
  if (argc > 1) {
    QuarterTracker tracker;
    double q, imported, epoch, mono;
    while (std::cin >> q >> imported >> epoch >> mono) {
      auto reading = tracker.feed(q, imported, epoch, mono);
      if (reading) std::cout << reading->accumulated_w << ' ' << reading->sample_epoch << '\n';
      else std::cout << "invalid\n";
    }
    return 0;
  }
  EnergyPolicyConfig c;
  double grid, battery, epoch, q, sampled;
  int minute, has_quarter;
  while (std::cin >> c.threshold_w >> c.max_charge_w >> c.max_discharge_w
         >> c.charge_start_minute >> c.charge_end_minute
         >> c.night_charge_enabled >> c.quarter_enabled >> c.response_margin_s >> c.night_charge_power_w
         >> grid >> battery >> epoch >> minute >> has_quarter >> q >> sampled) {
    auto result = decide_energy(c, grid, battery, epoch,
        minute < 0 ? std::nullopt : std::optional<int>(minute),
        has_quarter ? std::optional<QuarterReading>({q, sampled}) : std::nullopt);
    if (!result) {
      std::cout << "invalid\n";
      continue;
    }
    const auto &d = *result;
    std::cout << d.battery_target_w << ' ' << d.grid_target_w << ' '
              << d.correction_w << ' ' << d.grid_limit_w << ' '
              << d.night_window << ' ' << d.quarter_used << ' ' << d.limited << '\n';
  }
}
