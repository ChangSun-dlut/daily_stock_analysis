import axios from 'axios';
import { API_BASE_URL } from '../utils/constants';
import { attachParsedApiError } from './error';

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.request.use((config) => {
  const url = config.url ?? '';
  // 分享图相关接口可能因报告内容多、后端转图慢、自动修复 gateway 而超过默认 30s，兜底延长超时。
  // 单条生成/推送 60s（推送含修复重试 180s）；批量生成/推送 120s（推送含修复重试 240s）。
  const isShareImage = url.includes('/share-image');
  const isBatch = url.includes('/share-image/batch');
  if (isShareImage && config.timeout === 30000) {
    config.timeout = isBatch ? 240000 : 180000;
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const path = window.location.pathname + window.location.search;
      if (!path.startsWith('/login')) {
        const redirect = encodeURIComponent(path);
        window.location.assign(`/login?redirect=${redirect}`);
      }
    }
    attachParsedApiError(error);
    return Promise.reject(error);
  }
);

export default apiClient;
