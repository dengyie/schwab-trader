import { useEffect, useRef } from 'react';

/**
 * 元素进入视口时触发一次回调（迷你图/富化数据懒加载用）。
 *
 * 返回 ref 挂到行元素上；IntersectionObserver 不存在或元素从未挂载时，
 * 回调在挂载后直接触发（降级为立即加载，宁可多请求不缺列）。
 * 触发一次后自动停止观察。
 */
export function useInViewOnce<T extends HTMLElement>(onInView: () => void) {
  const ref = useRef<T | null>(null);
  const firedRef = useRef(false);
  // 回调经 ref 转发，避免调用方把每帧变化的函数写进依赖导致观察器重建。
  const cbRef = useRef(onInView);
  cbRef.current = onInView;

  useEffect(() => {
    const el = ref.current;
    if (!el || firedRef.current) return;

    if (typeof IntersectionObserver === 'undefined') {
      firedRef.current = true;
      cbRef.current();
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting || firedRef.current) continue;
          firedRef.current = true;
          observer.disconnect();
          cbRef.current();
        }
      },
      { rootMargin: '200px' }, // 提前 200px 预取，滚动到时图已就绪
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return ref;
}
