import { fetcher } from "./client";

// This public response contains only available lease limits, never scoring data.
export interface ApplicationPolicy {
  gpu_max_lease_days: number;
  cpu_max_lease_days: number;
}

export async function fetchApplicationPolicy(): Promise<ApplicationPolicy> {
  const policy = await fetcher<ApplicationPolicy>("/containers/application-policy").catch(() => {
    throw new Error("使用期限加载失败，请重试");
  });
  if (!policy || ![policy.gpu_max_lease_days, policy.cpu_max_lease_days].every((days) => Number.isSafeInteger(days) && days >= 1)) {
    throw new Error("使用期限加载失败，请重试");
  }
  return {
    gpu_max_lease_days: policy.gpu_max_lease_days,
    cpu_max_lease_days: policy.cpu_max_lease_days,
  };
}
