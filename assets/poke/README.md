# 戳一戳奖励图片资源

三张模板使用内置 **image_gen** 工具于 2026-11-03 生成，未使用 CLI / API key。参考本工作区投喂插件已有、由用户提供的可可子参考形象生成的角色图；角色特征沿用浅绿色双马尾、彩虹皇冠与发夹、红橙眼睛、蓝色耳机和水手服。原参考图未复制进本插件。

- [happy.png](happy.png)：开心。
- [wink.png](wink.png)：眨眼。
- [pout.png](pout.png)：鼓脸。

模板牌面有意留白，`poke_card.py` 在运行时渲染实际积分，避免生成模型写错数字或修改配置后图文不一致。三个表情独立随机选择，文字始终显示本次已到账金额。模板均为不透明奶油色背景，运行时输出 768×768 PNG。

`fonts/RewardSans.otf` 是基于 Noto Sans CJK SC 派生的 Feed Sans 再次裁剪得到的字体，保留「积分+0123456789」，新字体名为 Access Gate Reward Sans。保留上游版权信息，使用 [SIL Open Font License 1.1](fonts/OFL.txt)。上游：[Noto CJK](https://github.com/notofonts/noto-cjk)。字体与模板都随插件发布，运行时无需从其他插件目录读取。

## 最终提示词

### 开心：角色参考生成

> Use case: illustration-story. Asset type: square reward-card template for the QQ bot 可可子. The provided image is a CHARACTER IDENTITY reference, not an edit target. Generate one polished cute chibi anime sticker illustration of the SAME girl: pale pistachio-green long twin-tail hair, magenta ribbons, tiny rainbow-striped crown with gold band, rainbow diamond hair clip, ruby-red eyes with orange lower irises, dark headphones with cyan rims around neck, white sailor blouse with coral-red collar and vivid cyan bow, navy skirt. Expression: delighted open-mouth smile, sparkling happy eyes and rosy cheeks. Front-facing upper body behind a large sign, both hands holding its left and right outer edges. Composition: a 1024 by 1024 square, entire crown and hair inside frame, the girl's head occupies upper half, sign is straight-on and perfectly horizontal centered in the LOWER third. The clean ivory sign face should span approximately x=180..844 and y=650..900, with a thin rounded warm-brown border, enough BLANK uninterrupted central space for runtime Chinese text. Hands stay on the OUTER edges; no fingers or hair across the blank central sign face. A few tiny soft gold sparkle accents outside the sign. Warm cream background, clean bold dark outlines, soft cel shading, adorable readable expression, crisp high-quality illustration. The sign MUST be completely BLANK because the program will print the actual changing reward value there. No letters, numbers, symbols, logos or watermarks anywhere. No extra characters.

### 眨眼：编辑开心模板，保留构图

> Use case: identity-preserve. Edit the provided reward-card illustration of 可可子 into another expression variant. Keep the SAME character identity, costume, rainbow crown, mint twin-tail hair, ribbons, headphones, colors, size, camera framing, hand positions, and cream background. Preserve the exact POSITION, SHAPE and SIZE of the large blank ivory sign at the bottom; its center must remain fully BLANK for the program to print a reward. No text or numbers, no watermarks. Change only her facial expression and the small mood accents beside her head. Clean bold outlines and soft cel shading matching the original. Expression: playful wink with one eye closed and the other red-orange eye sparkling, cheerful small open smile and soft rosy cheeks; small golden star accents. Sweet and mischievous, clearly different from the original wide-eyed smile.

### 鼓脸：编辑开心模板，保留构图

> Use case: identity-preserve. Edit the provided reward-card illustration of 可可子 into another expression variant. Keep the SAME character identity, costume, rainbow crown, mint twin-tail hair, ribbons, headphones, colors, size, camera framing, hand positions, and cream background. Preserve the exact POSITION, SHAPE and SIZE of the large blank ivory sign at the bottom; its center must remain fully BLANK for the program to print a reward. No text or numbers, no watermarks. Change only her facial expression and the small mood accents beside her head. Clean bold outlines and soft cel shading matching the original. Expression: cute mildly annoyed pout, puffed rosy cheeks, eyebrows tilted in a mock-serious way, both red-orange eyes looking directly at viewer. One tiny stylized steam puff beside her head instead of a sparkle. Still adorable and friendly, as if she was poked but is reluctantly giving a reward. No tears, no threatening gestures.
