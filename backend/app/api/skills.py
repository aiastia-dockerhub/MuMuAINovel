"""Skill 聊天 API

提供 Skill 列表查询和基于 Skill 的流式聊天功能。
用户选择一个 Skill 后，以该 Skill 的工作流指令作为系统提示词进行对话。
"""
from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional, List, Dict

from app.database import get_db
from app.user_manager import User
from app.api.settings import require_login
from app.services.skill_loader import get_all_skills_cached, get_skill_by_trigger, get_skill_detail, create_skill_files, update_skill_files, delete_skill_files, refresh_skills_cache
from app.services.ai_service import AIService, create_user_ai_service
from app.utils.sse_response import SSEResponse, create_sse_response, wrap_stream_with_heartbeat, HEARTBEAT
from app.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/skills", tags=["Skills"])


class SkillChatRequest(BaseModel):
    """Skill 聊天请求"""
    skill_key: str  # SKILL_STORY_LONG_WRITE 等
    message: str    # 用户消息
    history: Optional[List[dict]] = None  # 历史对话 [{"role": "user/assistant", "content": "..."}]


class SkillApplyChapterRequest(BaseModel):
    """对已有章节应用 Skill"""
    chapter_id: str
    skill_key: str
    model: Optional[str] = None  # 可选自定义模型


class SkillCreateRequest(BaseModel):
    """创建 Skill 请求"""
    name: str           # Skill 名称（英文，如 my-new-skill）
    description: str    # Skill 描述
    body: str           # 工作流指令（Markdown 正文）
    references: Optional[Dict[str, str]] = None  # 参考知识库 {"文件名": "内容"}
    skill_type: Optional[str] = None  # Skill 类型：writing/polishing/analysis/tool/generic


class SkillUpdateRequest(BaseModel):
    """更新 Skill 请求"""
    description: Optional[str] = None
    body: Optional[str] = None
    references: Optional[Dict[str, str]] = None
    skill_type: Optional[str] = None  # Skill 类型：writing/polishing/analysis/tool/generic


@router.get("/list")
async def list_skills(user: User = Depends(require_login)):
    """获取所有可用 Skill 列表"""
    skills = get_all_skills_cached()
    return [
        {
            "template_key": s["template_key"],
            "template_name": s["template_name"],
            "category": s["category"],
            "skill_type": s.get("skill_type", "generic"),
            "category_hint": s.get("category_hint", ""),
            "description": s["description"],
            "triggers": s.get("triggers", []),
        }
        for s in skills
    ]


@router.post("/match")
async def match_skill(request: Request, user: User = Depends(require_login)):
    """根据用户输入匹配最合适的 Skill"""
    body = await request.json()
    user_input = body.get("user_input", "")

    if not user_input:
        return {"matched": False}

    skill = get_skill_by_trigger(user_input)
    if skill:
        return {
            "matched": True,
            "skill": {
                "template_key": skill["template_key"],
                "template_name": skill["template_name"],
                "category": skill["category"],
                "description": skill["description"],
            }
        }
    return {"matched": False}


@router.post("/chat")
async def skill_chat(
    request: SkillChatRequest,
    user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    """
    基于 Skill 的流式聊天

    接收用户消息和 Skill 标识，以 Skill 内容作为系统提示词，
    通过用户的 AI 配置进行流式回复。
    """
    # 查找 Skill
    skills = get_all_skills_cached()
    skill = None
    for s in skills:
        if s["template_key"] == request.skill_key:
            skill = s
            break

    if not skill:
        async def error_gen():
            yield await SSEResponse.send_error(f"未找到 Skill: {request.skill_key}")
        return create_sse_response(error_gen())

    # 获取系统提示词（Skill 内容）
    system_prompt = skill["content"]

    # 构建完整提示词（将历史消息拼接到提示词中）
    history_text = ""
    if request.history:
        for msg in request.history[-20:]:
            role_label = "用户" if msg.get("role") == "user" else "助手"
            history_text += f"\n{role_label}: {msg.get('content', '')}"

    full_prompt = request.message
    if history_text:
        full_prompt = f"以下是之前的对话历史：{history_text}\n\n用户最新消息: {request.message}"

    # 获取用户 AI 配置
    from app.api.settings import get_user_ai_service
    try:
        ai_service = await get_user_ai_service(user=user, db=db)
        # 覆盖系统提示词为 Skill 内容
        ai_service.default_system_prompt = system_prompt
    except Exception as e:
        logger.error(f"创建 AI 服务失败: {e}")
        async def error_gen():
            yield await SSEResponse.send_error(f"AI 服务配置错误: {str(e)}")
        return create_sse_response(error_gen())

    # 流式生成
    async def generate():
        try:
            yield await SSEResponse.send_progress(f"正在使用 {skill['template_name']}...", 10)

            stream = ai_service.generate_text_stream(
                prompt=full_prompt,
                system_prompt=system_prompt,
                auto_mcp=False,  # Skill 聊天不使用 MCP 工具
            )

            async for item in wrap_stream_with_heartbeat(stream, heartbeat_interval=15.0):
                if item is HEARTBEAT:
                    yield await SSEResponse.send_heartbeat()
                    continue
                yield await SSEResponse.send_chunk(item)

            yield await SSEResponse.send_progress("回复完成", 100, "success")
            yield await SSEResponse.send_done()

        except Exception as e:
            logger.error(f"Skill 聊天生成失败: {e}")
            yield await SSEResponse.send_error(f"生成失败: {str(e)}")

    return create_sse_response(generate())


@router.post("/apply-to-chapter")
async def apply_skill_to_chapter(
    request_body: SkillApplyChapterRequest,
    http_request: Request,
    user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    """
    对已有章节应用 Skill（流式返回）

    工作流程：
    1. 加载章节内容
    2. 以 Skill 内容作为系统提示词
    3. 将章节内容发给 AI 处理
    4. 流式返回处理结果
    5. 自动保存回章节
    """
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.models.chapter import Chapter
    from app.models.project import Project
    from app.api.common import verify_project_access
    from app.api.settings import get_user_ai_service

    user_id = getattr(http_request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    # 查找 Skill
    skills = get_all_skills_cached()
    skill = None
    for s in skills:
        if s["template_key"] == request_body.skill_key:
            skill = s
            break

    if not skill:
        raise HTTPException(status_code=404, detail=f"未找到 Skill: {request_body.skill_key}")

    # 获取章节
    result = await db.execute(
        select(Chapter).where(Chapter.id == request_body.chapter_id)
    )
    chapter = result.scalar_one_or_none()
    if not chapter:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="章节不存在")

    # 验证权限
    await verify_project_access(chapter.project_id, user_id, db)

    if not chapter.content or chapter.content.strip() == "":
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="章节内容为空")

    skill_content = skill["content"]
    skill_name = skill["template_name"]
    chapter_content = chapter.content

    logger.info(f"🔧 对章节 {request_body.chapter_id} 应用 Skill '{skill_name}'（{len(chapter_content)}字）")

    # 获取 AI 服务
    try:
        ai_service = await get_user_ai_service(user=user, db=db)
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"AI 服务配置错误: {str(e)}")

    # 构建提示词
    system_prompt = skill_content
    user_prompt = f"请对以下章节内容执行处理，只做局部修改，不要整段重写。直接输出处理后的完整正文，不要任何解释。\n\n{chapter_content}"

    # 计算 max_tokens
    max_tokens = max(2000, min(int(len(chapter_content) * 1.5), 16000))

    generate_kwargs = {
        "prompt": user_prompt,
        "system_prompt": system_prompt,
        "max_tokens": max_tokens,
    }
    if request_body.model:
        generate_kwargs["model"] = request_body.model

    async def generate():
        import asyncio
        full_content = ""
        chunk_count = 0

        try:
            yield await SSEResponse.send_progress(f"正在使用 {skill_name} 处理章节...", 10)

            async for chunk in ai_service.generate_text_stream(**generate_kwargs):
                full_content += chunk
                chunk_count += 1
                yield await SSEResponse.send_chunk(chunk)

                # 每20个chunk发送心跳
                if chunk_count % 20 == 0:
                    yield await SSEResponse.send_heartbeat()

                await asyncio.sleep(0)

            # 自动保存回章节
            if full_content.strip():
                # 需要新的数据库会话来保存
                from app.database import get_engine
                from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as NewAsyncSession

                engine = await get_engine(user_id)
                AsyncSessionLocal = async_sessionmaker(engine, class_=NewAsyncSession, expire_on_commit=False)
                async with AsyncSessionLocal() as save_db:
                    result = await save_db.execute(
                        select(Chapter).where(Chapter.id == request_body.chapter_id)
                    )
                    ch = result.scalar_one_or_none()
                    if ch:
                        old_word_count = ch.word_count or 0
                        new_word_count = len(full_content.strip())
                        ch.content = full_content.strip()
                        ch.word_count = new_word_count

                        # 更新项目字数
                        proj_result = await save_db.execute(
                            select(Project).where(Project.id == ch.project_id)
                        )
                        proj = proj_result.scalar_one_or_none()
                        if proj:
                            proj.current_words = (proj.current_words or 0) - old_word_count + new_word_count

                        await save_db.commit()
                        logger.info(f"✅ Skill '{skill_name}' 处理完成并保存：{old_word_count}字 → {new_word_count}字")

                yield await SSEResponse.send_event("saved", {
                    "word_count": len(full_content.strip()),
                    "message": f"处理完成，已自动保存"
                })

            yield await SSEResponse.send_progress("处理完成", 100, "success")
            yield await SSEResponse.send_done()

        except Exception as e:
            logger.error(f"Skill 处理章节失败: {e}")
            yield await SSEResponse.send_error(f"处理失败: {str(e)}")

    return create_sse_response(generate())


# ==================== Skill 管理 CRUD API ====================

@router.get("/detail/{skill_key:path}")
async def get_skill_detail_api(skill_key: str, user: User = Depends(require_login)):
    """获取 Skill 详细信息（包括原始内容和 references）"""
    detail = get_skill_detail(skill_key)
    if not detail:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"未找到 Skill: {skill_key}")
    
    return {
        "template_key": detail["template_key"],
        "template_name": detail["template_name"],
        "category": detail["category"],
        "skill_type": detail.get("skill_type", "generic"),
        "category_hint": detail.get("category_hint", ""),
        "description": detail["description"],
        "triggers": detail.get("triggers", []),
        "raw_content": detail.get("raw_content", ""),
        "standalone_references": detail.get("standalone_references", {}),
    }


@router.post("/create")
async def create_skill(request: SkillCreateRequest, user: User = Depends(require_login)):
    """创建新的 Skill"""
    try:
        result = create_skill_files(
            name=request.name,
            description=request.description,
            body=request.body,
            references=request.references,
            skill_type=request.skill_type,
        )
        return {"success": True, "skill": result}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"创建 Skill 失败: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"创建失败: {str(e)}")


@router.put("/update/{skill_key:path}")
async def update_skill(skill_key: str, request: SkillUpdateRequest, user: User = Depends(require_login)):
    """更新 Skill"""
    try:
        result = update_skill_files(
            skill_key=skill_key,
            description=request.description,
            body=request.body,
            references=request.references,
            skill_type=request.skill_type,
        )
        return {"success": True, "skill": result}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"更新 Skill 失败: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")


@router.delete("/delete/{skill_key:path}")
async def delete_skill(skill_key: str, user: User = Depends(require_login)):
    """删除 Skill"""
    try:
        delete_skill_files(skill_key)
        return {"success": True, "message": f"已删除 Skill: {skill_key}"}
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"删除 Skill 失败: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


@router.post("/refresh-cache")
async def refresh_cache(user: User = Depends(require_login)):
    """手动刷新 Skill 缓存"""
    skills = refresh_skills_cache()
    return {"success": True, "count": len(skills)}
