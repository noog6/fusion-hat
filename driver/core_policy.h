#ifndef FUSION_HAT_CORE_POLICY_H
#define FUSION_HAT_CORE_POLICY_H

static inline bool fusion_hat_core_policy_valid(bool experimental, bool shutdown_disabled,
                                               bool restricted)
{
    return !experimental || (shutdown_disabled && restricted);
}

static inline unsigned int fusion_hat_control_mode(bool restricted)
{
    return restricted ? 0644 : 0666;
}

#endif
