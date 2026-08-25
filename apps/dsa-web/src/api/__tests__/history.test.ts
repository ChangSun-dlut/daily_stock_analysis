import { beforeEach, describe, expect, it, vi } from 'vitest';
import { historyApi } from '../history';

const get = vi.hoisted(() => vi.fn());
const post = vi.hoisted(() => vi.fn());

vi.mock('../index', () => ({
  default: {
    get,
    post,
    put: vi.fn(),
  },
}));

describe('historyApi.pushBatchShareImage', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it('posts record_ids / cards_per_row / channel and normalizes the response', async () => {
    post.mockResolvedValueOnce({
      data: {
        success: true,
        message: '已通过 OpenClaw 推送分享图到微信',
        pushed: true,
      },
    });

    const result = await historyApi.pushBatchShareImage([17, 18, 19], 3, 'openclaw_wechat');

    expect(post).toHaveBeenCalledTimes(1);
    const [url, body] = post.mock.calls[0];
    expect(url).toBe('/api/v1/history/share-image/batch/push');
    expect(body).toEqual({
      record_ids: [17, 18, 19],
      cards_per_row: 3,
      channel: 'openclaw_wechat',
    });
    expect(result).toEqual({
      success: true,
      message: '已通过 OpenClaw 推送分享图到微信',
      pushed: true,
    });
  });

  it('defaults channel to openclaw_wechat and cards_per_row to 3', async () => {
    post.mockResolvedValueOnce({
      data: { success: false, message: '推送失败', pushed: false },
    });

    const result = await historyApi.pushBatchShareImage([17, 18]);

    const [, body] = post.mock.calls[0];
    expect(body.cards_per_row).toBe(3);
    expect(body.channel).toBe('openclaw_wechat');
    expect(result.success).toBe(false);
    expect(result.pushed).toBe(false);
    expect(result.message).toBe('推送失败');
  });
});
