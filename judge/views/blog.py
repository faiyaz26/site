from django.core.urlresolvers import reverse
from django.http import Http404
from django.utils import timezone
from django.views.generic import ListView, DetailView
from judge.comments import CommentedDetailView
from judge.models import BlogPost
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.views import TitleMixin


class PostList(ListView):
    model = BlogPost
    paginate_by = 10
    context_object_name = 'posts'
    template_name = 'blog/list.jade'
    title = None

    def get_paginator(self, queryset, per_page, orphans=0,
                      allow_empty_first_page=True, **kwargs):
        return DiggPaginator(queryset, per_page, body=6, padding=2,
                             orphans=orphans, allow_empty_first_page=allow_empty_first_page, **kwargs)

    def get_queryset(self):
        return BlogPost.objects.filter(visible=True, publish_on__lte=timezone.now())

    def get_context_data(self, **kwargs):
        context = super(PostList, self).get_context_data(**kwargs)
        context['title'] = self.title or 'Page %d of Posts' % context['page_obj'].number
        context['first_page_href'] = reverse('home')
        return context


class PostView(TitleMixin, CommentedDetailView):
    model = BlogPost
    pk_url_kwarg = 'id'
    context_object_name = 'post'
    template_name = 'blog/content.jade'

    def get_title(self):
        return self.object.title

    def get_comment_page(self):
        return 'b:%s' % self.object.id

    def get_object(self, queryset=None):
        post = super(PostView, self).get_object(queryset)
        if (not post.visible or post.publish_on > timezone.now())\
                and not self.request.user.has_perm('judge.see_hidden_post'):
            raise Http404()
        return post