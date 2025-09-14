async def _do_publish(user_id: int, items: List[Dict[str, Any]], caption: str, album_ocr_on: bool):
    """Реальная отправка сообщений (фото/видео/альбомы/текст/форвард)."""
    if not items:
        return

    # Спец-случай: форвардим оригинал (чтобы эмоджи остались активными)
    if items and items[0].get("kind") == "forward":
        it = items[0]
        try:
            await bot.forward_message(
                chat_id=TARGET_CHAT_ID,
                from_chat_id=it["from_chat_id"],
                message_id=it["mid"],
            )
        except Exception:
            # если форвард по какой-то причине не удался — fallback в обычный текст
            if caption:
                await bot.send_message(TARGET_CHAT_ID, caption)
        return

    # текстовый пост «как есть»
    if items and items[0].get("kind") == "text":
        await bot.send_message(TARGET_CHAT_ID, caption or "")
        return

    # OCR-фильтрация только для альбомов при album_ocr_on=True
    items = await filter_pricetag_media(items, album_ocr_on)

    if len(items) == 1:
        it = items[0]
        if it["kind"] == "video":
            await bot.send_video(TARGET_CHAT_ID, it["fid"], caption=caption)
        else:
            await bot.send_photo(TARGET_CHAT_ID, it["fid"], caption=caption)
        return

    # Альбом: подпись к первому
    first = items[0]
    media = []
    if first["kind"] == "video":
        media.append(InputMediaVideo(media=first["fid"], caption=caption, parse_mode=ParseMode.HTML))
    else:
        media.append(InputMediaPhoto(media=first["fid"], caption=caption, parse_mode=ParseMode.HTML))
    for it in items[1:]:
        media.append(InputMediaVideo(media=it["fid"]) if it["kind"] == "video" else InputMediaPhoto(media=it["fid"]))
    await bot.send_media_group(TARGET_CHAT_ID, media)
